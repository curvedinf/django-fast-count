import hashlib
import os
import time
from datetime import timedelta
from django.core.cache import cache
from django.db import connections
from django.db.models import Manager
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
                 manager_instance=None,  # New primary way to configure
                 # Direct config/override kwargs:
                 manager_name=None,
                 precache_count_every=None,
                 cache_counts_larger_than=None,
                 expire_cached_counts_after=None,
                 precache_lock_timeout=None):

        actual_model = model
        actual_using = using

        # Tentative values for FC settings from direct kwargs
        actual_manager_name = manager_name
        actual_precache_count_every = precache_count_every
        actual_cache_counts_larger_than = cache_counts_larger_than
        actual_expire_cached_counts_after = expire_cached_counts_after
        actual_precache_lock_timeout = precache_lock_timeout

        if manager_instance:
            actual_model = manager_instance.model  # manager_instance dictates model/using
            actual_using = manager_instance._db

            # If direct kwargs were None (not explicitly passed to override), populate from manager_instance.
            # The manager_instance attributes (e.g., manager_instance.precache_count_every)
            # are already defaulted by FastCountManager.__init__.
            if actual_manager_name is None:
                actual_manager_name = manager_instance._get_own_name_on_model()
            if actual_precache_count_every is None:
                actual_precache_count_every = manager_instance.precache_count_every
            if actual_cache_counts_larger_than is None:
                actual_cache_counts_larger_than = manager_instance.cache_counts_larger_than
            if actual_expire_cached_counts_after is None:
                actual_expire_cached_counts_after = manager_instance.expire_cached_counts_after
            if actual_precache_lock_timeout is None:
                actual_precache_lock_timeout = manager_instance.precache_lock_timeout
        else:
            # No manager_instance provided, rely purely on direct kwargs or apply library defaults.
            # Apply DEFAULT_XXX constants if the corresponding kwarg was None.
            if actual_precache_count_every is None:
                actual_precache_count_every = DEFAULT_PRECACHE_COUNT_EVERY
            if actual_cache_counts_larger_than is None:
                actual_cache_counts_larger_than = DEFAULT_CACHE_COUNTS_LARGER_THAN
            if actual_expire_cached_counts_after is None:
                actual_expire_cached_counts_after = DEFAULT_EXPIRE_CACHED_COUNTS_AFTER

            # Special defaulting for precache_lock_timeout if not provided by manager_instance or direct kwarg
            if actual_precache_lock_timeout is None:
                # Use already determined actual_precache_count_every for calculation
                interval_for_lock_calc = actual_precache_count_every
                actual_precache_lock_timeout = max(
                    300, int(interval_for_lock_calc.total_seconds() * 1.5)
                )
            elif isinstance(actual_precache_lock_timeout, timedelta):
                actual_precache_lock_timeout = int(actual_precache_lock_timeout.total_seconds())
            else:  # Assuming int
                actual_precache_lock_timeout = int(actual_precache_lock_timeout)

        # Critical for QuerySet base class
        if actual_model is None:
            raise TypeError("FastCountQuerySet initialized without 'model' or 'manager_instance'.")

        super().__init__(actual_model, query, actual_using, hints)

        # Final assignment to self
        self.manager_name = actual_manager_name
        self.precache_count_every = actual_precache_count_every
        self.cache_counts_larger_than = actual_cache_counts_larger_than
        self.expire_cached_counts_after = actual_expire_cached_counts_after
        self.precache_lock_timeout = actual_precache_lock_timeout

        # Cache key templates, dependent on manager_name which is now part of QS state
        self._precache_last_run_key_template = "fastcount:last_precache:{ct_id}:{manager}"
        self._precache_lock_key_template = "fastcount:lock_precache:{ct_id}:{manager}"

    def _clone(self, **kwargs):
        """
        Create a clone of this QuerySet, ensuring that custom FastCount attributes
        are propagated to the new instance.
        """
        clone = super()._clone(**kwargs)
        # Propagate custom attributes.
        # These attributes are set by FastCountManager.get_queryset() or __init__.
        # If type(self) in super()._clone() calls our __init__ with these args,
        # this might be redundant for some, but it's safer to ensure they are set.
        # However, QuerySet._clone typically calls type(self)(...) without these custom kwargs.

        # Pass all FastCount specific attributes to the clone's constructor.
        # This ensures the clone is also a fully configured FastCountQuerySet.
        # Note: _clone calls type(self)(model, query, using, hints). We are adding our custom args.
        # Django's QuerySet._clone calls:
        # klass = kwargs.pop("__klass", type(self))
        # c = klass(model=self.model, query=self.query.chain(klass=klass), using=self._db, hints=self._hints)
        # So our custom __init__ needs to handle being called by _clone as well.
        # The current __init__ design should handle this: `model` and `using` will be passed by _clone.
        # The fc_settings will be None unless explicitly passed in kwargs to _clone itself (which we don't do here).
        # However, we want the clone to *inherit* these from the original.
        # So, we explicitly set them on the clone after super()._clone() has created it.
        # This is safer than trying to inject into kwargs for _clone's internal call to type(self).

        clone.manager_name = self.manager_name
        clone.precache_count_every = self.precache_count_every
        clone.cache_counts_larger_than = self.cache_counts_larger_than
        clone.expire_cached_counts_after = self.expire_cached_counts_after
        clone.precache_lock_timeout = self.precache_lock_timeout

        # Ensure key templates are also on the clone
        clone._precache_last_run_key_template = self._precache_last_run_key_template
        clone._precache_lock_key_template = self._precache_lock_key_template
        return clone

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
        # Create a base .all() queryset using this QuerySet's type and configuration.
        base_all_qs = type(self)(
            model=self.model,  # Pass model and using for clarity for type(self) call
            using=self.db,
            manager_instance=None,  # Explicitly None, rely on direct fc_settings from self
            manager_name=self.manager_name,
            precache_count_every=self.precache_count_every,
            cache_counts_larger_than=self.cache_counts_larger_than,
            expire_cached_counts_after=self.expire_cached_counts_after,
            precache_lock_timeout=self.precache_lock_timeout
        ).all()  # .all() will then call _clone, which now propagates these attrs.

        querysets_to_precache = [base_all_qs]
        method = getattr(self.model, "fast_count_querysets", None)
        if method and callable(method):
            try:
                custom_querysets = method()
                if isinstance(custom_querysets, (list, tuple)):
                    # Ensure these custom querysets are also correctly configured
                    # If they were created like `cls.objects.filter(...)`, our _clone fix handles it.
                    querysets_to_precache.extend(custom_querysets)
                else:
                    print(
                        f"Warning: {self.model.__name__}.fast_count_querysets did not return a list or tuple."
                    )
            except TypeError as e:
                if "missing 1 required positional argument" in str(e) or \
                        "takes 0 positional arguments but 1 was given" in str(e):  # Python 3.10+
                    print(
                        f"Warning: {self.model.__name__}.fast_count_querysets seems to be an instance method "
                        f"(error: {e}). Consider making it a @classmethod or @staticmethod."
                    )
                else:
                    print(
                        f"Error calling or processing fast_count_querysets for {self.model.__name__}: {e}"
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
        from .models import FastCount  # Dynamically import to avoid circular dependency
        # Ensure all config attributes are present before proceeding
        if not all([
            self.manager_name, self.model,
            self.precache_count_every, self.cache_counts_larger_than,
            self.expire_cached_counts_after, self.precache_lock_timeout
        ]):
            print(
                f"Warning: precache_counts called on a FastCountQuerySet for {getattr(self.model, '__name__', 'UnknownModel')} "
                f"with missing configuration. Aborting precache for this queryset/manager."
            )
            return {}

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
            # Ensure the qs_to_precache is also fully configured; get_precache_querysets and _clone should handle this.
            if not hasattr(qs_to_precache, 'manager_name') or not qs_to_precache.manager_name:
                print(
                    f"Warning: Skipping a queryset in precache_counts for {self.model.__name__} due to missing manager_name configuration on it.")
                continue

            cache_key = self._get_cache_key(qs_to_precache)
            try:
                # Use a basic QuerySet for the actual count to avoid recursion or unintended side effects
                # from our custom count() method if qs_to_precache was somehow this instance again.
                # The query itself is what matters for the count.
                base_qs_for_count = QuerySet(
                    model=qs_to_precache.model, query=qs_to_precache.query.clone(), using=qs_to_precache.db
                )
                actual_count = base_qs_for_count.count()

                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=qs_to_precache.manager_name,  # Use manager_name from the specific qs being processed
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
                    f"  - Precached {model_ct} ({qs_to_precache.manager_name}) hash {cache_key[:8]}...: {actual_count}"
                )
            except Exception as e:
                # Use manager_name from self (the primary QS instance this method was called on) for the error message context
                print(
                    f"Error precaching count for {self.model.__name__} (manager: {self.manager_name}) queryset ({cache_key}): {e}"
                )
                results[cache_key] = f"Error: {e}"
        print(
            f"Precaching finished for {model_ct} (manager: {self.manager_name}). {len(results)} querysets processed."
        )
        return results

    def maybe_trigger_precache(self):
        """
        Checks if enough time has passed since the last precache run for this
        manager and model, and forks a background process to run precache_counts if needed.
        Uses cache locking to prevent multiple forks.
        If `DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING` env var is set, runs synchronously.
        """
        if not all([
            self.manager_name, self.model,
            self.precache_count_every, self.cache_counts_larger_than,
            self.expire_cached_counts_after, self.precache_lock_timeout
        ]):
            # Don't attempt to trigger precache if config is missing.
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
                    self.precache_counts()  # self is already a fully configured FastCountQuerySet
                    cache.set(last_run_key, time.time(), None)  # Set timeout to None for indefinite cache
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
                    pass  # Optionally re-raise
                return

            pid = os.fork()
            if pid > 0:  # Parent process
                print(
                    f"Forked background precache process {pid} for {model_ct} ({self.manager_name})."
                )
                return
            else:  # Child process
                connections.close_all()
                print(
                    f"Background precache process (PID {os.getpid()}) starting for {model_ct} ({self.manager_name})."
                )
                child_error = None
                try:
                    # In the child, `self` refers to the same configured FastCountQuerySet instance
                    self.precache_counts()
                    cache.set(last_run_key, time.time(), None)  # Set timeout to None for indefinite cache
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
        from .models import FastCount  # Dynamically import to avoid circular dependency

        if not all([
            hasattr(self, 'manager_name') and self.manager_name,  # Check attribute existence first
            hasattr(self, 'precache_count_every') and self.precache_count_every is not None,
            hasattr(self, 'cache_counts_larger_than') and self.cache_counts_larger_than is not None,
            hasattr(self, 'expire_cached_counts_after') and self.expire_cached_counts_after is not None,
            hasattr(self, 'precache_lock_timeout') and self.precache_lock_timeout is not None
        ]):
            # If essential configurations are missing, fall back to default count.
            print(
                f"Warning: FastCountQuerySet for {self.model.__name__} is missing configuration. Falling back to standard count."
            )
            return super().count()

        cache_key = self._get_cache_key()  # Uses `self` as the queryset
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
            pass  # Fall through to actual count

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
                        "is_precached": False,  # Retroactively cached
                    },
                )
            except Exception as e:
                print(
                    f"Error retroactively caching count in DB for {self.model.__name__} ({self.manager_name}, {cache_key}): {e}"
                )
            if expires_seconds > 0:
                cache.set(cache_key, actual_count, int(expires_seconds))
        return actual_count


class FastCountManager(Manager):
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
            # Default lock timeout is 1.5x the precache interval, or 5 mins (300s), whichever is greater.
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
        # This warning can be noisy if managers are assigned dynamically in ways hard to detect.
        # Consider making it less prominent or context-dependent if it becomes an issue.
        # For now, it's useful for debugging manager name resolution.
        print(
            f"Warning: Could not determine manager name for {model_name_str} (manager instance: {self}). Falling back to 'objects'."
        )
        return "objects"  # Fallback default

    def get_queryset(self):
        """
        Returns an instance of FastCountQuerySet (or a subclass specified by
        the manager, e.g., in a testapp), configured by this manager.
        Derived managers should override this method to return their specific
        QuerySet class, passing `manager_instance=self`.
        e.g., `return MyCustomFastCountQuerySet(manager_instance=self)`
        """
        # This default implementation returns FastCountQuerySet.
        # Subclasses of FastCountManager that use a different QuerySet class
        # (like IntermediateFastCountManager using IntermediateFastCountQuerySet)
        # should override get_queryset to instantiate their specific QuerySet class,
        # passing `manager_instance=self` to its constructor.
        return FastCountQuerySet(manager_instance=self)

    # The count() method on the manager itself is convenient.
    # It will use the configured FastCountQuerySet's count method.
    def count(self):
        """
        Returns the count of all objects managed by this manager, potentially
        using a cached value. Delegates to the FastCountQuerySet's count method.
        This will also trigger the potential background precaching.
        """
        return self.all().count()