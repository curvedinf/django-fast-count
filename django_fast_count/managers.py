import hashlib
import os
import time
from datetime import timedelta
from django.core.cache import cache
from django.db import models, connections
from django.db.models.query import QuerySet
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
# Avoid circular import by importing late or using string reference if needed
# from .models import FastCount

DEFAULT_PRECACHE_COUNT_EVERY = timedelta(minutes=10)
DEFAULT_CACHE_COUNTS_LARGER_THAN = 1_000_000
DEFAULT_EXPIRE_CACHED_COUNTS_AFTER = timedelta(minutes=10)
# Environment variable to disable forking, useful for testing
DISABLE_FORK_ENV_VAR = "DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING"

class FastCountQuerySet(QuerySet):
    """
    A QuerySet subclass that overrides count() to use cached values and
    potentially trigger background precaching.
    It also encapsulates the logic for precaching and cache key generation.
    """

    def __init__(self, model=None, query=None, using=None, hints=None, 
                 manager_name=None, 
                 precache_count_every=None, 
                 cache_counts_larger_than=None, 
                 expire_cached_counts_after=None, 
                 precache_lock_timeout=None):
        super().__init__(model, query, using, hints)
        self.manager_name = manager_name
        self.precache_count_every = precache_count_every
        self.cache_counts_larger_than = cache_counts_larger_than
        self.expire_cached_counts_after = expire_cached_counts_after
        self.precache_lock_timeout = precache_lock_timeout

        # Cache key templates, dependent on manager_name which is now part of QS state
        self._precache_last_run_key_template = "fastcount:last_precache:{ct_id}:{manager}"
        self._precache_lock_key_template = "fastcount:lock_precache:{ct_id}:{manager}"

    def _get_cache_key(self, queryset_to_key=None):
        """
        Generates a unique and stable cache key for a given queryset based on
        its model and the SQL query it represents.
        If queryset_to_key is None, `self` is used.
        """
        qs_for_key = queryset_to_key if queryset_to_key is not None else self
        try:
            # Use the SQL query and parameters for a robust key
            sql, params = qs_for_key.query.get_compiler(using=qs_for_key.db).as_sql()
            # Include model name to prevent collisions between different models
            key_string = f"{qs_for_key.model.__module__}.{qs_for_key.model.__name__}:{sql}:{params}"
            # Use MD5 for a reasonably short and collision-resistant hash
            return hashlib.md5(key_string.encode("utf-8")).hexdigest()
        except Exception as e:
            # Fallback if SQL generation fails (should be rare)
            print(
                f"Warning: Could not generate precise cache key for {qs_for_key.model.__name__} using SQL. Error: {e}"
            )
            # Use a less precise key based on the query object representation
            key_string = (
                f"{qs_for_key.model.__module__}.{qs_for_key.model.__name__}:{repr(qs_for_key.query)}"
            )
            return f"fallback:{hashlib.md5(key_string.encode('utf-8')).hexdigest()}"

    def get_precache_querysets(self):
        """
        Retrieves the list of querysets designated for precaching counts.
        Starts with the default `.all()` queryset (created with this QS's config)
        and adds any querysets returned by the model's `fast_count_querysets` method.
        """
        # Create a base .all() queryset using this QuerySet's type and configuration
        base_all_qs = type(self)(
            model=self.model, 
            using=self.db,
            manager_name=self.manager_name,
            precache_count_every=self.precache_count_every,
            cache_counts_larger_than=self.cache_counts_larger_than,
            expire_cached_counts_after=self.expire_cached_counts_after,
            precache_lock_timeout=self.precache_lock_timeout
        ).all()
        querysets_to_precache = [base_all_qs]
        
        method = getattr(self.model, "fast_count_querysets", None)
        if method and callable(method):
            try:
                custom_querysets = method()
                if isinstance(custom_querysets, (list, tuple)):
                    querysets_to_precache.extend(custom_querysets)
                else:
                    print(
                        f"Warning: {self.model.__name__}.fast_count_querysets did not return a list or tuple."
                    )
            except TypeError as e:
                if "missing 1 required positional argument" in str(e):
                    print(
                        f"Warning: {self.model.__name__}.fast_count_querysets seems to be an instance method "
                        f"(error: {e}). Consider making it a @classmethod or @staticmethod."
                    )
                else:
                    print(
                        f"Error calling fast_count_querysets for {self.model.__name__}: {e}"
                    )
            except Exception as e:
                print(
                    f"Error calling or processing fast_count_querysets for {self.model.__name__}: {e}"
                )
        return querysets_to_precache

    def precache_counts(self):
        """
        Calculates and caches counts for all designated precache querysets.
        This method is intended to be called periodically, either by the
        background fork triggered by .count() or a management command.
        """
        from .models import FastCount # Dynamically import to avoid circular dependency
        
        model_ct = ContentType.objects.get_for_model(self.model)
        querysets = self.get_precache_querysets()
        now = timezone.now()
        expiry_time = now + self.expire_cached_counts_after
        expires_seconds = self.expire_cached_counts_after.total_seconds()
        results = {}
        
        print(
            f"Precaching started for {model_ct} (manager: {self.manager_name}) at {now.isoformat()}"
        )
        for qs_to_precache in querysets:
            cache_key = self._get_cache_key(qs_to_precache)
            try:
                base_qs_for_count = models.QuerySet(
                    model=qs_to_precache.model, query=qs_to_precache.query.clone(), using=qs_to_precache.db
                )
                actual_count = base_qs_for_count.count()
                
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=self.manager_name,
                    queryset_hash=cache_key,
                    defaults={
                        "count": actual_count,
                        "last_updated": now,
                        "expires_at": expiry_time,
                        "is_precached": True,
                    },
                )
                if expires_seconds > 0:
                    cache.set(cache_key, actual_count, int(expires_seconds))
                results[cache_key] = actual_count
                print(
                    f"  - Precached {model_ct} ({self.manager_name}) hash {cache_key[:8]}...: {actual_count}"
                )
            except Exception as e:
                print(
                    f"Error precaching count for {self.model.__name__} (manager: {self.manager_name}) queryset ({cache_key}): {e}"
                )
                results[cache_key] = f"Error: {e}"
        print(
            f"Precaching finished for {model_ct} ({self.manager_name}). {len(results)} querysets processed."
        )
        return results

    def maybe_trigger_precache(self):
        """
        Checks if enough time has passed since the last precache run for this
        manager and model, and forks a background process to run precache_counts if needed.
        Uses cache locking to prevent multiple forks.
        If `DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING` env var is set, runs synchronously.
        """
        if not self.precache_count_every or self.manager_name is None:
            return

        model_ct = ContentType.objects.get_for_model(self.model)
        last_run_key = self._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=self.manager_name
        )
        lock_key = self._precache_lock_key_template.format(
            ct_id=model_ct.id, manager=self.manager_name
        )
        now_ts = time.time()
        last_run_ts = cache.get(last_run_key)

        if last_run_ts and (
            now_ts < last_run_ts + self.precache_count_every.total_seconds()
        ):
            return

        lock_acquired = cache.add(lock_key, "running", self.precache_lock_timeout)
        if not lock_acquired:
            print(
                f"Precache lock {lock_key} not acquired. Process for {model_ct} ({self.manager_name}) already running or recently finished/failed."
            )
            return

        try:
            if os.environ.get(DISABLE_FORK_ENV_VAR):
                print(
                    f"SYNC_TEST_MODE: Forking disabled. Running precache_counts synchronously for {model_ct} ({self.manager_name})."
                )
                sync_error = None
                try:
                    self.precache_counts()
                    cache.set(last_run_key, time.time(), None)
                    print(
                        f"SYNC_TEST_MODE: precache_counts finished synchronously for {model_ct} ({self.manager_name})."
                    )
                except Exception as e:
                    sync_error = e
                    print(
                        f"SYNC_TEST_MODE: Error in synchronous precache_counts for {model_ct} ({self.manager_name}): {e}"
                    )
                finally:
                    cache.delete(lock_key)
                if sync_error:
                    pass # Optionally re-raise
                return

            pid = os.fork()
            if pid > 0: # Parent process
                print(
                    f"Forked background precache process {pid} for {model_ct} ({self.manager_name})."
                )
                return
            else: # Child process
                connections.close_all()
                print(
                    f"Background precache process (PID {os.getpid()}) starting for {model_ct} ({self.manager_name})."
                )
                child_error = None
                try:
                    self.precache_counts()
                    cache.set(last_run_key, time.time(), None)
                    print(
                        f"Background precache process (PID {os.getpid()}) finished successfully."
                    )
                except Exception as e:
                    child_error = e
                    print(
                        f"Background precache process (PID {os.getpid()}) failed: {e}"
                    )
                finally:
                    cache.delete(lock_key)
                    os._exit(0 if child_error is None else 1)
        except OSError as e:
            print(
                f"Error forking/managing precache process for {model_ct} ({self.manager_name}): {e}"
            )
            cache.delete(lock_key)
        except Exception as e:
            print(
                f"Unexpected error during precache trigger for {model_ct} ({self.manager_name}): {e}"
            )
            cache.delete(lock_key)

    def count(self):
        """
        Provides a count of objects matching the QuerySet, potentially using
        a cached value from Django's cache or the FastCount database table.
        Falls back to the original database count if no valid cache entry is found.
        Retroactively caches large counts.
        Triggers background precaching if configured and needed.
        """
        from .models import FastCount # Dynamically import to avoid circular dependency

        if not self.manager_name or \
           self.precache_count_every is None or \
           self.cache_counts_larger_than is None or \
           self.expire_cached_counts_after is None or \
           self.precache_lock_timeout is None:
            # If essential configurations are missing, fall back to default count.
            # This might happen if FastCountQuerySet is instantiated directly without proper setup.
            print(
                f"Warning: FastCountQuerySet for {self.model.__name__} is missing configuration. Falling back to standard count."
            )
            return super().count()

        cache_key = self._get_cache_key() # Uses `self` as the queryset
        model_ct = ContentType.objects.get_for_model(self.model)
        now = timezone.now()

        # 1. Check Django's cache
        cached_count = cache.get(cache_key)
        if cached_count is not None:
            self.maybe_trigger_precache()
            return cached_count

        # 2. Check DB cache (FastCount model)
        try:
            db_cache_entry = FastCount.objects.using(self.db).get(
                content_type=model_ct,
                manager_name=self.manager_name,
                queryset_hash=cache_key,
                expires_at__gt=now,
            )
            expires_seconds = (db_cache_entry.expires_at - now).total_seconds()
            if expires_seconds > 0:
                cache.set(
                    cache_key,
                    db_cache_entry.count,
                    int(expires_seconds),
                )
            self.maybe_trigger_precache()
            return db_cache_entry.count
        except FastCount.DoesNotExist:
            pass
        except Exception as e:
            print(
                f"Error checking FastCount DB cache for {self.model.__name__} ({self.manager_name}, {cache_key}): {e}"
            )
            pass

        # 3. Perform actual count using the database
        actual_count = super().count()
        self.maybe_trigger_precache()

        # 4. Retroactively cache if the count meets the threshold
        if actual_count >= self.cache_counts_larger_than:
            expiry_time = now + self.expire_cached_counts_after
            expires_seconds = self.expire_cached_counts_after.total_seconds()
            try:
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=self.manager_name,
                    queryset_hash=cache_key,
                    defaults={
                        "count": actual_count,
                        "last_updated": now,
                        "expires_at": expiry_time,
                        "is_precached": False,
                    },
                )
            except Exception as e:
                print(
                    f"Error retroactively caching count in DB for {self.model.__name__} ({self.manager_name}, {cache_key}): {e}"
                )
            if expires_seconds > 0:
                cache.set(cache_key, actual_count, int(expires_seconds))
        
        return actual_count


class FastCountManager(models.Manager):
    """
    A model manager that returns FastCountQuerySet instances, configured
    for fast counting and background precaching.
    """
    def __init__(
        self,
        precache_count_every=None,
        cache_counts_larger_than=None,
        expire_cached_counts_after=None,
        precache_lock_timeout=None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.precache_count_every = (
            precache_count_every
            if precache_count_every is not None
            else DEFAULT_PRECACHE_COUNT_EVERY
        )
        self.cache_counts_larger_than = (
            cache_counts_larger_than
            if cache_counts_larger_than is not None
            else DEFAULT_CACHE_COUNTS_LARGER_THAN
        )
        self.expire_cached_counts_after = (
            expire_cached_counts_after
            if expire_cached_counts_after is not None
            else DEFAULT_EXPIRE_CACHED_COUNTS_AFTER
        )
        if precache_lock_timeout is None:
            self.precache_lock_timeout = max(
                300, int(self.precache_count_every.total_seconds() * 1.5)
            )
        elif isinstance(precache_lock_timeout, timedelta):
            self.precache_lock_timeout = int(precache_lock_timeout.total_seconds())
        else:
            self.precache_lock_timeout = int(precache_lock_timeout)

    def _get_own_name_on_model(self):
        """Tries to find the name this manager instance is assigned to on its model."""
        if hasattr(self, "model") and self.model:
            # Check standard managers defined directly on the class
            for name, attr in self.model.__dict__.items():
                if attr is self:
                    return name
            # Check managers defined via _meta or dynamically added
            if hasattr(self.model, "_meta") and hasattr(
                self.model._meta, "managers_map"
            ):
                for name, mgr_instance in self.model._meta.managers_map.items():
                    if mgr_instance is self:
                        return name
        model_name_str = self.model.__name__ if hasattr(self, "model") and self.model else "UnknownModel"
        print(
            f"Warning: Could not determine manager name for {model_name_str} (manager instance: {self}). Falling back to 'objects'."
        )
        return "objects"


    def get_queryset(self):
        """
        Returns an instance of FastCountQuerySet, configured with this manager's settings.
        """
        manager_name = self._get_own_name_on_model()
        return FastCountQuerySet(
            self.model, 
            using=self._db,
            manager_name=manager_name,
            precache_count_every=self.precache_count_every,
            cache_counts_larger_than=self.cache_counts_larger_than,
            expire_cached_counts_after=self.expire_cached_counts_after,
            precache_lock_timeout=self.precache_lock_timeout
        )

    # The count() method on the manager itself is convenient.
    # It will use the configured FastCountQuerySet's count method.
    def count(self):
        """
        Returns the count of all objects managed by this manager, potentially
        using a cached value. Delegates to the FastCountQuerySet's count method.
        This will also trigger the potential background precaching.
        """
        return self.all().count()
