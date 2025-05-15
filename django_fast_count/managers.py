import hashlib
import os
import time
import subprocess  # New import
import sys  # New import
from datetime import timedelta
from django.core.cache import cache
from django.db import connections
from django.db.models import Manager
from django.db.models.query import QuerySet
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.conf import settings  # New import for settings.BASE_DIR

# Avoid circular import by importing late or using string reference if needed
# from .models import FastCount
DEFAULT_PRECACHE_COUNT_EVERY = timedelta(minutes=10)
DEFAULT_CACHE_COUNTS_LARGER_THAN = 1_000_000
DEFAULT_EXPIRE_CACHED_COUNTS_AFTER = timedelta(minutes=10)

# Environment variable to disable forking/background processing and force synchronous execution.
# Old name, kept for backward compatibility for a version.
_DISABLE_FORK_ENV_VAR_OLD_NAME = "DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING"
# New preferred name.
FORCE_SYNC_PRECACHE_ENV_VAR = "DJANGO_FAST_COUNT_FORCE_SYNC_PRECACHE"


class FastCountQuerySet(QuerySet):
    """
    A QuerySet subclass that overrides count() to use cached values and
    potentially trigger background precaching.
    It also encapsulates the logic for precaching and cache key generation.
    """

    def __init__(
            self,
            model=None,
            query=None,
            using=None,
            hints=None,
            manager_instance=None,  # New primary way to configure
            # Direct config/override kwargs:
            manager_name=None,
            precache_count_every=None,
            cache_counts_larger_than=None,
            expire_cached_counts_after=None,
            precache_lock_timeout=None,
    ):
        actual_model = model
        actual_using = using
        # Tentative values for FC settings from direct kwargs
        actual_manager_name = manager_name
        actual_precache_count_every = precache_count_every
        actual_cache_counts_larger_than = cache_counts_larger_than
        actual_expire_cached_counts_after = expire_cached_counts_after
        actual_precache_lock_timeout = precache_lock_timeout

        if manager_instance:
            actual_model = (
                manager_instance.model
            )  # manager_instance dictates model/using
            actual_using = manager_instance._db
            # If direct kwargs were None (not explicitly passed to override), populate from manager_instance.
            # The manager_instance attributes (e.g., manager_instance.precache_count_every)
            # are already defaulted by FastCountManager.__init__.
            if actual_manager_name is None:
                actual_manager_name = manager_instance._get_own_name_on_model()
            if actual_precache_count_every is None:
                actual_precache_count_every = manager_instance.precache_count_every
            if actual_cache_counts_larger_than is None:
                actual_cache_counts_larger_than = (
                    manager_instance.cache_counts_larger_than
                )
            if actual_expire_cached_counts_after is None:
                actual_expire_cached_counts_after = (
                    manager_instance.expire_cached_counts_after
                )
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
                actual_precache_lock_timeout = int(
                    actual_precache_lock_timeout.total_seconds()
                )
            else:  # Assuming int
                actual_precache_lock_timeout = int(actual_precache_lock_timeout)

        # Critical for QuerySet base class
        if actual_model is None:
            raise TypeError(
                "FastCountQuerySet initialized without 'model' or 'manager_instance'."
            )

        super().__init__(actual_model, query, actual_using, hints)

        # Final assignment to self
        self.manager_name = actual_manager_name
        self.precache_count_every = actual_precache_count_every
        self.cache_counts_larger_than = actual_cache_counts_larger_than
        self.expire_cached_counts_after = actual_expire_cached_counts_after
        self.precache_lock_timeout = actual_precache_lock_timeout

        # Cache key templates, dependent on manager_name which is now part of QS state
        self._precache_last_run_key_template = (
            "fastcount:last_precache:{ct_id}:{manager}"
        )
        self._precache_lock_key_template = "fastcount:lock_precache:{ct_id}:{manager}"

    def _clone(self, **kwargs):
        """
        Create a clone of this QuerySet, ensuring that custom FastCount attributes
        are propagated to the new instance.
        """
        clone = super()._clone(**kwargs)
        clone.manager_name = self.manager_name
        clone.precache_count_every = self.precache_count_every
        clone.cache_counts_larger_than = self.cache_counts_larger_than
        clone.expire_cached_counts_after = self.expire_cached_counts_after
        clone.precache_lock_timeout = self.precache_lock_timeout
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
            sql, params = qs_for_key.query.get_compiler(using=qs_for_key.db).as_sql()
            key_string = f"{qs_for_key.model.__module__}.{qs_for_key.model.__name__}:{sql}:{params}"
            return hashlib.md5(key_string.encode("utf-8")).hexdigest()
        except Exception as e:
            print(
                f"Warning: Could not generate precise cache key for {qs_for_key.model.__name__} using SQL. Error: {e}"
            )
            key_string = f"{qs_for_key.model.__module__}.{qs_for_key.model.__name__}:{repr(qs_for_key.query)}"
            return f"fallback:{hashlib.md5(key_string.encode('utf-8')).hexdigest()}"

    def get_precache_querysets(self):
        """
        Retrieves the list of querysets designated for precaching counts.
        Starts with the default `.all()` queryset (created with this QS's config)
        and adds any querysets returned by the model's `fast_count_querysets` method.
        """
        base_all_qs = type(self)(
            model=self.model,
            using=self.db,
            manager_instance=None,
            manager_name=self.manager_name,
            precache_count_every=self.precache_count_every,
            cache_counts_larger_than=self.cache_counts_larger_than,
            expire_cached_counts_after=self.expire_cached_counts_after,
            precache_lock_timeout=self.precache_lock_timeout,
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
                if "missing 1 required positional argument" in str(
                        e
                ) or "takes 0 positional arguments but 1 was given" in str(
                    e
                ):
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
        This method is intended to be called periodically, either by a
        background process triggered by .count() or a management command.
        """
        from .models import FastCount
        if not all(
                [
                    self.manager_name,
                    self.model,
                    self.precache_count_every,
                    self.cache_counts_larger_than,
                    self.expire_cached_counts_after,
                    self.precache_lock_timeout,
                ]
        ):
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
            f"Precaching started for {self.model.__name__} (manager: {self.manager_name}) at {now.isoformat()}"
        )

        for qs_to_precache in querysets:
            if (
                    not hasattr(qs_to_precache, "manager_name")
                    or not qs_to_precache.manager_name
            ):
                print(
                    f"Warning: Skipping a queryset in precache_counts for {self.model.__name__} due to missing manager_name configuration on it."
                )
                continue
            cache_key = self._get_cache_key(qs_to_precache)
            try:
                base_qs_for_count = QuerySet(
                    model=qs_to_precache.model,
                    query=qs_to_precache.query.clone(),
                    using=qs_to_precache.db,
                )
                actual_count = base_qs_for_count.count()
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=qs_to_precache.manager_name,
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
                    f"  - Precached {self.model.__name__} ({qs_to_precache.manager_name}) hash {cache_key[:8]}...: {actual_count}"
                )
            except Exception as e:
                print(
                    f"Error precaching count for {self.model.__name__} (manager: {self.manager_name}) queryset ({cache_key}): {e}"
                )
                results[cache_key] = f"Error: {e}"
        print(
            f"Precaching finished for {self.model.__name__} (manager: {self.manager_name}). {len(results)} querysets processed."
        )
        return results

    def maybe_trigger_precache(self):
        """
        Checks if enough time has passed since the last precache run for this
        manager and model. If needed, it triggers `precache_counts`.
        If `DJANGO_FAST_COUNT_FORCE_SYNC_PRECACHE` env var is set, runs synchronously.
        Otherwise, launches `manage.py precache_fast_counts` as a background subprocess.
        Uses cache locking to prevent multiple triggers for the same model/manager.
        """
        if not all(
                [
                    self.manager_name,
                    self.model,
                    self.precache_count_every,
                    self.cache_counts_larger_than,
                    self.expire_cached_counts_after,
                    self.precache_lock_timeout,
                ]
        ):
            return

        model_name_for_log = getattr(self.model, "__name__", "UnknownModel")
        manager_name_for_log = self.manager_name or "UnknownManager"

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
                f"Precache lock {lock_key} not acquired for {model_name_for_log} ({manager_name_for_log}). "
                f"Process already running or recently finished/failed."
            )
            return

        update_last_run_and_release_lock = True  # Assume success by default

        try:
            # Check new and old env var names for synchronous mode
            force_sync_mode = os.environ.get(FORCE_SYNC_PRECACHE_ENV_VAR) or \
                              os.environ.get(_DISABLE_FORK_ENV_VAR_OLD_NAME)

            if force_sync_mode:
                print(
                    f"SYNC_MODE: Running precache_counts synchronously for {model_name_for_log} ({manager_name_for_log})."
                )
                sync_error = None
                try:
                    self.precache_counts()
                    print(
                        f"SYNC_MODE: precache_counts finished synchronously for {model_name_for_log} ({manager_name_for_log})."
                    )
                except Exception as e:
                    sync_error = e
                    print(
                        f"SYNC_MODE: Error in synchronous precache_counts for {model_name_for_log} ({manager_name_for_log}): {e}"
                    )
                if sync_error:
                    update_last_run_and_release_lock = False  # Don't update last_run if sync failed
            else:  # Background subprocess mode
                manage_py_path = os.path.join(settings.BASE_DIR, "manage.py")
                cmd = [sys.executable, manage_py_path, "precache_fast_counts"]
                # Note: The current `precache_fast_counts` command updates all models.
                # To make it specific, it would need to accept model/manager args, e.g.:
                # cmd.extend(["--model", f"{self.model._meta.app_label}.{self.model.__name__}",
                #             "--manager", self.manager_name])

                print(
                    f"Attempting to launch background precache command for {model_name_for_log} ({manager_name_for_log}). "
                    f"Cmd: \"{' '.join(cmd)}\""
                )
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True  # Detach on POSIX
                        # For Windows, consider: creationflags=subprocess.DETACHED_PROCESS
                    )
                    print(
                        f"Launched background precache process (PID {process.pid}) for {model_name_for_log} ({manager_name_for_log})."
                    )
                except Exception as e:
                    print(
                        f"Error launching background precache command for {model_name_for_log} ({manager_name_for_log}): {e}"
                    )
                    update_last_run_and_release_lock = False  # Don't update last_run if launch failed
        except Exception as e:  # Catch any other unexpected errors in the try block
            print(
                f"Unexpected error during precache trigger for {model_name_for_log} ({manager_name_for_log}): {e}"
            )
            update_last_run_and_release_lock = False
        finally:
            if update_last_run_and_release_lock:
                cache.set(last_run_key, time.time(), None)  # None means persist indefinitely
            cache.delete(lock_key)  # Always release the lock

        return  # Parent process returns immediately after launching or sync completion attempt

    def count(self):
        """
        Provides a count of objects matching the QuerySet, potentially using
        a cached value from Django's cache or the FastCount database table.
        Falls back to the original database count if no valid cache entry is found.
        Retroactively caches large counts.
        Triggers background precaching if configured and needed.
        """
        from .models import FastCount
        if not all(
                [
                    hasattr(self, "manager_name")
                    and self.manager_name,
                    hasattr(self, "precache_count_every")
                    and self.precache_count_every is not None,
                    hasattr(self, "cache_counts_larger_than")
                    and self.cache_counts_larger_than is not None,
                    hasattr(self, "expire_cached_counts_after")
                    and self.expire_cached_counts_after is not None,
                    hasattr(self, "precache_lock_timeout")
                    and self.precache_lock_timeout is not None,
                ]
        ):
            print(
                f"Warning: FastCountQuerySet for {self.model.__name__} is missing configuration. Falling back to standard count."
            )
            return super().count()

        cache_key = self._get_cache_key()
        model_ct = ContentType.objects.get_for_model(self.model)
        now = timezone.now()

        cached_count = cache.get(cache_key)
        if cached_count is not None:
            self.maybe_trigger_precache()
            return cached_count

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

        actual_count = super().count()
        self.maybe_trigger_precache()

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
            for name, attr in self.model.__dict__.items():
                if attr is self:
                    return name
            if hasattr(self.model, "_meta") and hasattr(
                    self.model._meta, "managers_map"
            ):
                for name, mgr_instance in self.model._meta.managers_map.items():
                    if mgr_instance is self:
                        return name
        model_name_str = (
            self.model.__name__
            if hasattr(self, "model") and self.model
            else "UnknownModel"
        )
        print(
            f"Warning: Could not determine manager name for {model_name_str} (manager instance: {self}). Falling back to 'objects'."
        )
        return "objects"

    def get_queryset(self):
        """
        Returns an instance of FastCountQuerySet (or a subclass specified by
        the manager), configured by this manager.
        """
        return FastCountQuerySet(manager_instance=self)

    def count(self):
        """
        Returns the count of all objects managed by this manager, potentially
        using a cached value. Delegates to the FastCountQuerySet's count method.
        """
        return self.all().count()
