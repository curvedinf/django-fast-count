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
# Lock timeout slightly longer than default precache interval to prevent stale locks
# but short enough to recover if a process dies.
DEFAULT_PRECACHE_LOCK_TIMEOUT_SECONDS = int(
    DEFAULT_PRECACHE_COUNT_EVERY.total_seconds() * 1.5
)

# Environment variable to disable forking, useful for testing
DISABLE_FORK_ENV_VAR = "DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING"


class FastCountQuerySet(QuerySet):
    """
    A QuerySet subclass that overrides count() to use cached values and
    potentially trigger background precaching.
    """

    # The manager instance will be attached here by FastCountModelManager.get_queryset
    manager = None

    def _clone(self, **kwargs):
        """
        Ensure that the custom 'manager' attribute is propagated when cloning.
        """
        clone = super()._clone(**kwargs)
        clone.manager = self.manager
        return clone

    def _get_manager_name(self):
        """Tries to find the name this manager instance is assigned to on the model."""
        if self.manager and hasattr(self.manager, "model"):
            # Check standard managers defined directly on the class
            for name, attr in self.manager.model.__dict__.items():
                if attr is self.manager:
                    return name
            # Check managers defined via _meta or dynamically added
            if hasattr(self.manager.model, "_meta") and hasattr(
                self.manager.model._meta, "managers_map"
            ):
                for name, mgr_instance in self.manager.model._meta.managers_map.items():
                    if mgr_instance is self.manager:
                        return name
        # Fallback if the name cannot be determined dynamically
        # This fallback might be problematic if multiple FastCount managers exist
        # and the name cannot be derived. The precache/DB cache keys rely on it.
        print(
            f"Warning: Could not determine manager name for {self.model.__name__}. Falling back to 'objects'."
        )
        return "objects"

    def count(self):
        """
        Provides a count of objects matching the QuerySet, potentially using
        a cached value from Django's cache or the FastCount database table.
        Falls back to the original database count if no valid cache entry is found.
        Retroactively caches large counts.
        Triggers background precaching if configured and needed.
        """
        # Dynamically import FastCount to avoid circular dependency issues at import time
        from .models import FastCount

        if not self.manager or not issubclass(type(self.manager), FastCountModelManager):
            # Fallback to default count if manager is not set or not the right type/subclass
            return super().count()

        manager_name = self._get_manager_name()
        cache_key = self.manager._get_cache_key(self)
        model_ct = ContentType.objects.get_for_model(self.model)
        now = timezone.now()
        calculated_count = None

        # 1. Check Django's cache
        cached_count = cache.get(cache_key)
        if cached_count is not None:
            # Trigger potential precache *after* returning the cached count quickly
            self.manager.maybe_trigger_precache(
                manager_name=manager_name, model_ct=model_ct
            )
            return cached_count

        # 2. Check DB cache (FastCount model)
        try:
            db_cache_entry = FastCount.objects.using(self.db).get(
                content_type=model_ct,
                manager_name=manager_name,
                queryset_hash=cache_key,
                expires_at__gt=now,
            )
            # Cache miss in Django cache, but hit in DB cache. Populate Django cache.
            expires_seconds = (db_cache_entry.expires_at - now).total_seconds()
            if expires_seconds > 0:
                cache.set(
                    cache_key,
                    db_cache_entry.count,
                    int(expires_seconds),  # cache.set expects int
                )
            # Trigger potential precache *after* returning the DB cached count
            self.manager.maybe_trigger_precache(
                manager_name=manager_name, model_ct=model_ct
            )
            return db_cache_entry.count
        except FastCount.DoesNotExist:
            # Cache miss in both Django cache and DB cache (or expired)
            pass
        except Exception as e:
            # Log error ideally - e.g., database connection issue
            print(
                f"Error checking FastCount DB cache for {self.model.__name__} ({cache_key}): {e}"
            )
            pass  # Proceed to calculate the actual count

        # 3. Perform actual count using the database
        # Use super().count() to call the original QuerySet count method
        actual_count = super().count()
        calculated_count = actual_count  # Store for potential retroactive cache

        # Trigger potential precache *after* calculating the actual count
        # We do this regardless of whether we cache retroactively
        self.manager.maybe_trigger_precache(
            manager_name=manager_name, model_ct=model_ct
        )

        # 4. Retroactively cache if the count meets the threshold
        if actual_count >= self.manager.cache_counts_larger_than:
            expiry_time = now + self.manager.expire_cached_counts_after
            expires_seconds = self.manager.expire_cached_counts_after.total_seconds()

            # Store/update in DB cache
            try:
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=manager_name,
                    queryset_hash=cache_key,
                    defaults={
                        "count": actual_count,
                        "last_updated": now,  # `last_updated` might be auto_now=True in model
                        "expires_at": expiry_time,
                        "is_precached": False,  # Mark as retroactively cached
                    },
                )
            except Exception as e:
                # Log error - e.g., database constraint violation, connection issue
                print(
                    f"Error retroactively caching count in DB for {self.model.__name__} ({cache_key}): {e}"
                )

            # Store/update in Django cache
            if expires_seconds > 0:
                cache.set(cache_key, actual_count, int(expires_seconds))

        return actual_count


class FastCountModelManager(models.Manager):
    """
    A model manager that provides a faster count() implementation for large tables
    by utilizing cached counts (both precached and retroactively cached).
    It also triggers periodic background precaching via forked processes.
    """

    def __init__(
        self,
        precache_count_every=None,
        cache_counts_larger_than=None,
        expire_cached_counts_after=None,
        precache_lock_timeout=None,  # New option for lock duration
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

        # Ensure lock timeout is reasonable relative to precache frequency
        if precache_lock_timeout is None:
            self.precache_lock_timeout = max(
                300, int(self.precache_count_every.total_seconds() * 1.5)
            )  # At least 5 mins
        elif isinstance(precache_lock_timeout, timedelta):
            self.precache_lock_timeout = int(precache_lock_timeout.total_seconds())
        else:
            self.precache_lock_timeout = int(
                precache_lock_timeout
            )  # Assume seconds if int/float

        # Cache keys are defined here for consistency
        self._precache_last_run_key_template = (
            "fastcount:last_precache:{ct_id}:{manager}"
        )
        self._precache_lock_key_template = "fastcount:lock_precache:{ct_id}:{manager}"

    def get_queryset(self):
        """
        Returns an instance of FastCountQuerySet and attaches this manager instance
        to the queryset so it can access configuration like thresholds and timeouts.
        """
        qs = FastCountQuerySet(self.model, using=self._db)
        qs.manager = self  # Attach manager instance to the queryset
        return qs

    def _get_cache_key(self, queryset):
        """
        Generates a unique and stable cache key for a given queryset based on
        the model, manager name, and the SQL query it represents.
        """
        try:
            # Use the SQL query and parameters for a robust key
            sql, params = queryset.query.get_compiler(using=queryset.db).as_sql()
            # Include model name to prevent collisions between different models
            # Manager name is included separately in DB lookups/cache keys
            key_string = f"{self.model.__module__}.{self.model.__name__}:{sql}:{params}"
            # Use MD5 for a reasonably short and collision-resistant hash
            return hashlib.md5(key_string.encode("utf-8")).hexdigest()
        except Exception as e:
            # Fallback if SQL generation fails (should be rare)
            print(
                f"Warning: Could not generate precise cache key for {self.model.__name__} using SQL. Error: {e}"
            )
            # Use a less precise key based on the query object representation
            key_string = (
                f"{self.model.__module__}.{self.model.__name__}:{repr(queryset.query)}"
            )
            return f"fallback:{hashlib.md5(key_string.encode('utf-8')).hexdigest()}"

    def get_precache_querysets(self):
        """
        Retrieves the list of querysets designated for precaching counts.
        Starts with the default `.all()` queryset and adds any querysets returned
        by the model's `fast_count_querysets` method (if defined).

        Expects `fast_count_querysets` to be defined on the model, potentially
        as an instance method, classmethod, or staticmethod.
        ```python
        # As instance method (receives self=model instance, but usually not needed)
        # Use `self.__class__.objects` or specific manager name if needed
        def fast_count_querysets(self):
             return [self.__class__.objects.filter(is_active=True)]

        # As classmethod (receives cls=model class) - recommended
        @classmethod
        def fast_count_querysets(cls):
            return [cls.objects.filter(is_active=True)]

        # As staticmethod (receives no implicit first arg)
        @staticmethod
        def fast_count_querysets():
             # Need to get model class explicitly if needed, e.g. from apps registry
             # Or, if defined within the model class body, can reference model name
             return [YourModel.objects.filter(is_active=True)]
        ```
        """
        # Start with the default .all() queryset generated by *this* manager
        querysets_to_precache = [self.get_queryset().all()]

        # Check for the custom method on the model class
        method = getattr(self.model, "fast_count_querysets", None)
        if method and callable(method):
            try:
                # Call the method. It could be instance, class, or static.
                custom_querysets = method()  # Call without arguments
                if isinstance(custom_querysets, (list, tuple)):
                    querysets_to_precache.extend(custom_querysets)
                else:
                    print(
                        f"Warning: {self.model.__name__}.fast_count_querysets did not return a list or tuple."
                    )
            except TypeError as e:
                # Handle case where it might be an instance method expecting `self`
                # A common signature for this error is "func() missing 1 required positional argument: 'arg_name'"
                if "missing 1 required positional argument" in str(
                    e
                ):  # More generic check
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

    def precache_counts(self, manager_name="objects"):
        """
        Calculates and caches counts for all designated precache querysets.
        This method is intended to be called periodically, either by the
        background fork triggered by .count() or a management command.

        Args:
            manager_name (str): The attribute name the manager instance is assigned to
                                on the model (e.g., 'objects', 'active_objects'). This
                                is needed to correctly store/retrieve from the DB cache.
        """
        # Dynamically import FastCount to avoid circular dependency issues at import time
        from .models import FastCount

        model_ct = ContentType.objects.get_for_model(self.model)
        querysets = self.get_precache_querysets()
        now = timezone.now()
        expiry_time = now + self.expire_cached_counts_after
        expires_seconds = self.expire_cached_counts_after.total_seconds()
        results = {}
        print(
            f"Precaching started for {model_ct} ({manager_name}) at {now.isoformat()}"
        )  # Add logging

        for qs in querysets:
            # Regenerate cache key using this manager's context
            cache_key = self._get_cache_key(qs)
            try:
                # Perform the actual count directly against the database.
                # Create a base queryset clone to ensure we bypass any FastCountQuerySet caching.
                base_qs_for_count = models.QuerySet(
                    model=qs.model, query=qs.query.clone(), using=qs.db
                )
                actual_count = base_qs_for_count.count()

                # Store/update in DB cache
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=manager_name,
                    queryset_hash=cache_key,
                    defaults={
                        "count": actual_count,
                        "last_updated": now,  # `last_updated` might be auto_now=True
                        "expires_at": expiry_time,
                        "is_precached": True,  # Mark as precached
                    },
                )
                # Store/update in Django cache
                if expires_seconds > 0:
                    cache.set(cache_key, actual_count, int(expires_seconds))
                results[cache_key] = actual_count
                print(
                    f"  - Precached {model_ct} ({manager_name}) hash {cache_key[:8]}...: {actual_count}"
                )
            except Exception as e:
                # Log error - e.g., database issue during count or update
                print(
                    f"Error precaching count for {self.model.__name__} queryset ({cache_key}): {e}"
                )
                results[cache_key] = f"Error: {e}"
        print(
            f"Precaching finished for {model_ct} ({manager_name}). {len(results)} querysets processed."
        )
        return results

    def maybe_trigger_precache(self, manager_name, model_ct):
        """
        Checks if enough time has passed since the last precache run for this
        manager and forks a background process to run precache_counts if needed.
        Uses cache locking to prevent multiple forks.
        If `DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING` env var is set, runs synchronously.
        """
        if not self.precache_count_every:  # Feature disabled if interval is None/zero
            return

        last_run_key = self._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        )
        lock_key = self._precache_lock_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        )

        now_ts = time.time()  # Use timestamp for interval checks
        last_run_ts = cache.get(last_run_key)

        # Check if interval has passed
        if last_run_ts and (
            now_ts < last_run_ts + self.precache_count_every.total_seconds()
        ):
            # Not time yet
            return

        # Try to acquire lock (common for both forked and sync test mode)
        # In sync test mode, this mainly prevents re-entry if called rapidly in a test.
        lock_acquired = cache.add(lock_key, "running", self.precache_lock_timeout)

        if not lock_acquired:
            # Another process is already handling precaching (or recently finished/failed)
            # Or, in sync mode, another call within the test might have acquired it.
            print(
                f"Precache lock {lock_key} not acquired. Process for {model_ct} ({manager_name}) already running or recently finished/failed."
            )
            return

        try:
            if os.environ.get(DISABLE_FORK_ENV_VAR):
                # Synchronous mode for testing
                print(
                    f"SYNC_TEST_MODE: Forking disabled. Running precache_counts synchronously for {model_ct} ({manager_name})."
                )
                sync_error = None
                try:
                    self.precache_counts(manager_name=manager_name)
                    cache.set(
                        last_run_key, time.time(), None
                    )  # Update last run time on success
                    print(
                        f"SYNC_TEST_MODE: precache_counts finished synchronously for {model_ct} ({manager_name})."
                    )
                except Exception as e:
                    sync_error = e
                    print(
                        f"SYNC_TEST_MODE: Error in synchronous precache_counts for {model_ct} ({manager_name}): {e}"
                    )
                finally:
                    cache.delete(lock_key)  # Release lock
                    # In sync mode, we don't os._exit()
                if sync_error:
                    # Optionally re-raise if critical for test flow, or just log.
                    pass
                return

            # Forking mode (production)
            pid = os.fork()
            if pid > 0:
                # Parent process: Log fork and return immediately.
                # Lock will expire automatically or be cleared by child.
                print(
                    f"Forked background precache process {pid} for {model_ct} ({manager_name})."
                )
                # Parent does NOT release the lock here; child or timeout will.
                return
            else:
                # Child process: Run the precaching logic
                # Close inherited connections (CRUCIAL!)
                connections.close_all()
                print(
                    f"Background precache process (PID {os.getpid()}) starting for {model_ct} ({manager_name})."
                )
                child_error = None
                try:
                    # Run the actual precaching
                    self.precache_counts(manager_name=manager_name)
                    # Update last run time only on success
                    cache.set(
                        last_run_key, time.time(), None
                    )  # Persist indefinitely until next run
                    print(
                        f"Background precache process (PID {os.getpid()}) finished successfully."
                    )
                except Exception as e:
                    child_error = e
                    print(
                        f"Background precache process (PID {os.getpid()}) failed: {e}"
                    )
                finally:
                    # Ensure lock is released by child even if precache fails
                    cache.delete(lock_key)
                    # Exit child process cleanly, avoiding parent's cleanup
                    os._exit(0 if child_error is None else 1)
        except OSError as e:
            # Fork failed (or error in parent part of forking logic)
            print(
                f"Error forking/managing precache process for {model_ct} ({manager_name}): {e}"
            )
            cache.delete(lock_key)  # Attempt to release lock if acquired by parent
        except Exception as e:
            # Catch other potential errors during fork setup/parent logic
            print(
                f"Unexpected error during precache trigger for {model_ct} ({manager_name}): {e}"
            )
            cache.delete(lock_key)  # Attempt cleanup

    # Override count() on the manager itself for convenience, although most
    # users will call count() on a queryset instance.
    def count(self):
        """
        Returns the count of all objects managed by this manager, potentially
        using a cached value. Delegates to the FastCountQuerySet's count method.
        This will also trigger the potential background precaching.
        """
        # self.all() returns the FastCountQuerySet instance
        return self.all().count()