import hashlib
from datetime import timedelta

from django.core.cache import cache
from django.db import models
from django.db.models.query import QuerySet
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType

# Avoid circular import by importing late or using string reference if needed
# from .models import FastCount

DEFAULT_PRECACHE_COUNT_EVERY = timedelta(minutes=10)
DEFAULT_CACHE_COUNTS_LARGER_THAN = 1_000_000
DEFAULT_EXPIRE_CACHED_COUNTS_AFTER = timedelta(minutes=10)


class FastCountQuerySet(QuerySet):
    """
    A QuerySet subclass that overrides count() to use cached values.
    """
    # The manager instance will be attached here by FastCountModelManager.get_queryset
    manager = None

    def _get_manager_name(self):
        """Tries to find the name this manager instance is assigned to on the model."""
        if self.manager and hasattr(self.manager, 'model'):
            # Check standard managers defined directly on the class
            for name, attr in self.manager.model.__dict__.items():
                if attr is self.manager:
                    return name
            # Check managers defined via _meta or dynamically added
            if hasattr(self.manager.model, '_meta') and hasattr(self.manager.model._meta, 'managers_map'):
                 for name, mgr_instance in self.manager.model._meta.managers_map.items():
                     if mgr_instance is self.manager:
                         return name
        # Fallback if the name cannot be determined dynamically
        return "objects"

    def count(self):
        """
        Provides a count of objects matching the QuerySet, potentially using
        a cached value from Django's cache or the FastCount database table.
        Falls back to the original database count if no valid cache entry is found.
        Retroactively caches large counts.
        """
        # Dynamically import FastCount to avoid circular dependency issues at import time
        from .models import FastCount

        if not self.manager or not isinstance(self.manager, FastCountModelManager):
            # Fallback to default count if manager is not set or not the right type
            # This happens if the QuerySet was not created via the FastCountModelManager
            return super().count()

        manager_name = self._get_manager_name()
        cache_key = self.manager._get_cache_key(self)
        model_ct = ContentType.objects.get_for_model(self.model)
        now = timezone.now()

        # 1. Check Django's cache
        cached_count = cache.get(cache_key)
        if cached_count is not None:
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
                    expires_seconds,
                )
            return db_cache_entry.count
        except FastCount.DoesNotExist:
            # Cache miss in both Django cache and DB cache (or expired)
            pass
        except Exception as e:
            # Log error ideally - e.g., database connection issue
            print(f"Error checking FastCount DB cache for {self.model.__name__} ({cache_key}): {e}")
            pass # Proceed to calculate the actual count

        # 3. Perform actual count using the database
        # Use super().count() to call the original QuerySet count method
        actual_count = super().count()

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
                        "last_updated": now, # `last_updated` might be auto_now=True in model
                        "expires_at": expiry_time,
                    },
                 )
            except Exception as e:
                 # Log error - e.g., database constraint violation, connection issue
                 print(f"Error retroactively caching count in DB for {self.model.__name__} ({cache_key}): {e}")

            # Store/update in Django cache
            if expires_seconds > 0:
                cache.set(cache_key, actual_count, expires_seconds)

        return actual_count


class FastCountModelManager(models.Manager):
    """
    A model manager that provides a faster count() implementation for large tables
    by utilizing cached counts (both precached and retroactively cached).
    """
    def __init__(
        self,
        precache_count_every=None,
        cache_counts_larger_than=None,
        expire_cached_counts_after=None,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.precache_count_every = precache_count_every if precache_count_every is not None else DEFAULT_PRECACHE_COUNT_EVERY
        self.cache_counts_larger_than = cache_counts_larger_than if cache_counts_larger_than is not None else DEFAULT_CACHE_COUNTS_LARGER_THAN
        self.expire_cached_counts_after = expire_cached_counts_after if expire_cached_counts_after is not None else DEFAULT_EXPIRE_CACHED_COUNTS_AFTER

    def get_queryset(self):
        """
        Returns an instance of FastCountQuerySet and attaches this manager instance
        to the queryset so it can access configuration like thresholds and timeouts.
        """
        qs = FastCountQuerySet(self.model, using=self._db)
        qs.manager = self # Attach manager instance to the queryset
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
            # Manager name is included in the DB lookup, hash just needs query uniqueness for that model
            key_string = f"{self.model.__module__}.{self.model.__name__}:{sql}:{params}"
            # Use MD5 for a reasonably short and collision-resistant hash
            return hashlib.md5(key_string.encode('utf-8')).hexdigest()
        except Exception as e:
            # Fallback if SQL generation fails (should be rare)
            print(f"Warning: Could not generate precise cache key for {self.model.__name__} using SQL. Error: {e}")
            # Use a less precise key based on the query object representation
            key_string = f"{self.model.__module__}.{self.model.__name__}:{repr(queryset.query)}"
            return f"fallback:{hashlib.md5(key_string.encode('utf-8')).hexdigest()}"

    def get_precache_querysets(self):
        """
        Retrieves the list of querysets designated for precaching counts.
        Starts with the default `.all()` queryset and adds any querysets returned
        by the model's `fast_count_querysets` classmethod (if defined).

        Assumes `fast_count_querysets` is defined as a @classmethod on the model:
        ```python
        @classmethod
        def fast_count_querysets(cls):
            # Returns querysets based on cls.objects (default manager) or another manager
            return [
                cls.objects.filter(is_active=True),
                cls.objects.filter(is_active=False),
            ]
        ```
        """
        # Start with the default .all() queryset generated by *this* manager
        querysets_to_precache = [self.get_queryset().all()]

        # Check for the custom method on the model class
        method = getattr(self.model, "fast_count_querysets", None)
        if method and callable(method):
            try:
                # Call the classmethod/staticmethod
                custom_querysets = method()
                if isinstance(custom_querysets, (list, tuple)):
                    # Add the custom querysets. The subsequent count operation
                    # should use the appropriate manager's context if the model
                    # correctly assigns this manager instance.
                    querysets_to_precache.extend(custom_querysets)
                else:
                    print(f"Warning: {self.model.__name__}.fast_count_querysets did not return a list or tuple.")
            except Exception as e:
                # Log error - e.g., method signature mismatch, error within the method
                print(f"Error calling or processing fast_count_querysets for {self.model.__name__}: {e}")

        return querysets_to_precache

    def precache_counts(self, manager_name="objects"):
        """
        Calculates and caches counts for all designated precache querysets.
        This method is intended to be called periodically (e.g., by a management command).

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

        for qs in querysets:
            # Regenerate cache key using this manager's context
            cache_key = self._get_cache_key(qs)
            try:
                # Perform the actual count directly against the database.
                # Use the internal _count() method of the *original* QuerySet class
                # to bypass our own caching mechanism within this precache routine.
                # This ensures we always get the fresh count from the DB for precaching.
                # We need to access the original QuerySet class's method.
                # super(FastCountQuerySet, qs).count() might work if qs is guaranteed FastCountQuerySet
                # A safer way might be to get a plain queryset first?
                # Or just call qs.count() and rely on it eventually hitting the DB if cache is empty?
                # Let's try calling the original count directly if possible.
                # This is tricky. Let's just execute the count normally.
                # If the manager is correctly configured, qs.count() might hit the cache,
                # but for precaching, we *want* the DB value.
                # Let's create a base queryset and call count on that.
                base_qs_for_count = models.QuerySet(model=qs.model, query=qs.query.clone(), using=qs.db)
                actual_count = base_qs_for_count.count()

                # Store/update in DB cache
                FastCount.objects.using(self.db).update_or_create(
                    content_type=model_ct,
                    manager_name=manager_name,
                    queryset_hash=cache_key,
                    defaults={
                        "count": actual_count,
                        "last_updated": now, # `last_updated` might be auto_now=True
                        "expires_at": expiry_time,
                    },
                )

                # Store/update in Django cache
                if expires_seconds > 0:
                    cache.set(cache_key, actual_count, expires_seconds)

                results[cache_key] = actual_count

            except Exception as e:
                # Log error - e.g., database issue during count or update
                print(f"Error precaching count for {self.model.__name__} queryset ({cache_key}): {e}")
                results[cache_key] = f"Error: {e}"

        return results

    # Override count() on the manager itself for convenience, although most
    # users will call count() on a queryset instance.
    def count(self):
        """
        Returns the count of all objects managed by this manager, potentially
        using a cached value. Delegates to the FastCountQuerySet's count method.
        """
        # self.all() returns the FastCountQuerySet instance
        return self.all().count()
