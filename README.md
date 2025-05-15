# Django Fast Count
[![PyPI](https://img.shields.io/pypi/v/django-fast-count)](https://pypi.org/project/django-fast-count/)
[![GitHub license](https://img.shields.io/github/license/curvedinf/django-fast-count)](LICENSE)
[![GitHub last commit](https://img.shields.io/github/last-commit/curvedinf/django-fast-count)](https://github.com/curvedinf/django-fast-count/commits/main)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/django-fast-count)](https://pypi.org/project/django-fast-count/)
[![GitHub stars](https://img.shields.io/github/stars/curvedinf/django-fast-count)](https://github.com/curvedinf/django-fast-count/stargazers)
[![Ko-fi Link](kofi.webp)](https://ko-fi.com/A0A31B6VB6)

A fast [Django](https://djangoproject.com) `.count()` implementation for large tables.

## The Problem

For most databases, when a table grows to several million rows, the performance of the default `QuerySet.count()` can degrade significantly. This often becomes the slowest query in a view, sometimes by orders of magnitude. Since the Django admin app uses `.count()` on every list page, this can render the admin unusable for large tables.

## The Solution

`django-fast-count` provides a faster, plug-and-play, database-agnostic `.count()` implementation. It achieves this by strategically caching count results, using two main mechanisms:
1.  **Precaching**: Regularly caches counts for predefined querysets in the background.
2.  **Retroactive Caching**: Caches counts for any queryset if the result is large, immediately after the count is performed.

## Key Features

*   **Drop-in Replacement**: Simply replace your model's manager with `FastCountManager`.
*   **Configurable Caching**: Control cache duration, precache frequency, and thresholds.
*   **Background Precaching**: Precaching can be triggered to run in a background subprocess, minimizing impact on request-response cycles.
*   **Management Command**: Proactively precache counts and clean up expired entries.
*   **Extensible**: Designed for easy subclassing of `FastCountManager` and `FastCountQuerySet`.
*   **Django Cache Integration**: Leverages Django's cache framework for fast lookups before hitting the database cache.

## Installation

1.  Install the package:
    ```bash
    pip install django-fast-count
    ```
2.  Add `django_fast_count` and `django.contrib.contenttypes` to your `INSTALLED_APPS` in `settings.py`:
    ```python
    # settings.py
    INSTALLED_APPS = [
        # ... other apps
        "django.contrib.contenttypes",  # Required by django-fast-count
        "django_fast_count",
        # ... other apps
    ]
    ```
3.  Run migrations:
    ```bash
    python manage.py migrate
    ```
    
## Basic Usage

To activate fast counts for a model, replace its default manager with `FastCountManager`.
```python
# models.py
from datetime import timedelta
from django.db.models import Model, BooleanField
from django_fast_count.managers import FastCountManager
class YourModel(Model):
    your_field = BooleanField(default=False)
    is_active = BooleanField(default=True)
    
    # Replace 'objects' manager
    objects = FastCountManager(
        precache_count_every=timedelta(hours=1),      # Default: 10 minutes
        cache_counts_larger_than=100_000,           # Default: 1,000,000
        expire_cached_counts_after=timedelta(hours=2), # Default: 10 minutes
    )
    
    # Optional: Define specific querysets to precache
    @classmethod
    def fast_count_querysets(cls):
        """
        Returns a list of querysets whose counts will be precached.
        By default, only cls.objects.all() is precached if this method is not defined.
        """
        return [
            cls.objects.filter(your_field=True),
            cls.objects.filter(your_field=False),
            cls.objects.filter(is_active=True).filter(your_field=True),
        ]
```

### `FastCountManager` Parameters

When initializing `FastCountManager`:
*   `precache_count_every` (timedelta): How often the querysets defined in `fast_count_querysets` (and the default `.all()`) should be re-counted and their results cached. Defaults to `timedelta(minutes=10)`.
*   `cache_counts_larger_than` (int): If a `.count()` query (that isn't already precached) returns a result greater than or equal to this number, that count will be retroactively cached. Defaults to `1,000,000`.
*   `expire_cached_counts_after` (timedelta): How long a cached count (both precached and retroactively cached) should remain valid. Defaults to `timedelta(minutes=10)`.
*   `precache_lock_timeout` (timedelta or int seconds): The timeout for the cache lock used to prevent multiple precaching processes from running simultaneously. Defaults to 1.5 times `precache_count_every` or 300 seconds, whichever is greater.

### `fast_count_querysets(cls)`
This class method on your model allows you to specify a list of querysets that you want to be regularly precached.
*   If this method is not defined on your model, `django-fast-count` will only precache the count for `YourModel.objects.all()`.
*   The querysets returned by this method will have their counts calculated and stored during each precaching cycle.

## How it Works

### Caching Layers

`django-fast-count` uses a two-tier caching system for counts:
1.  **Django's Cache Framework**: The primary cache. When a count is requested, `django-fast-count` first checks Django's configured cache (e.g., Redis, Memcached). This is the fastest lookup.
2.  **Database Cache (`FastCount` model)**: If the count is not found in Django's cache, `django-fast-count` checks a dedicated database table (`django_fast_count_fastcount`). This table stores serialized counts, their hashes, and expiry times. If a valid entry is found here, it's used and also written back to Django's cache for future requests.
If a count is found in neither cache, the actual `COUNT(*)` query is executed against the database.

### Caching Mechanisms

*   **Precaching**:
    *   For querysets defined in `fast_count_querysets()` (and `YourModel.objects.all()`).
    *   Counts are updated periodically (defined by `precache_count_every`).
    *   This process is triggered automatically by any `.count()` call on the model or can be run manually via a management command.
*   **Retroactive Caching**:
    *   Applies to any `.count()` query performed on the model.
    *   If the actual count result is `cache_counts_larger_than` or more, the result is cached immediately after being calculated.
    *   This ensures that unexpectedly large, non-predefined counts also benefit from caching on subsequent requests.

### The `FastCount` Model

This model (`django_fast_count.models.FastCount`) stores the cached counts in your database. Key fields include:
*   `content_type`: Links to the model being counted.
*   `manager_name`: The name of the manager on the model (e.g., "objects").
*   `queryset_hash`: An MD5 hash of the SQL query, uniquely identifying the queryset.
*   `count`: The cached count value.
*   `last_updated`: Timestamp of the last cache update.
*   `expires_at`: Timestamp when the cache entry becomes stale.
*   `is_precached`: Boolean indicating if the entry was from precaching or retroactive caching.

### Cache Key Generation

A unique cache key is generated for each queryset based on:
*   The model's module and name.
*   The SQL query string generated by Django for the queryset.
*   The parameters used in the SQL query.
This string is then hashed (MD5) to create a stable `queryset_hash`.

## The Precaching Process

### Automatic Precaching (Background Subprocess)

*   When `YourModel.objects.count()` (or any count on a `FastCountManager`-backed queryset) is called, the system checks if it's time to run the precaching process for that model and manager (based on `precache_count_every`).
*   If precaching is due, and a lock can be acquired (to prevent multiple simultaneous runs), the `precache_fast_counts` **management command is launched as a background subprocess** (using `subprocess.Popen`).
*   This detached subprocess then executes the `precache_fast_counts` management command, which iterates through relevant models and managers to update their precached counts (querysets defined in `fast_count_querysets()` and the default `.all()` queryset).
*   **Note for Serverless Environments**: Since launching background subprocesses may not work reliably in serverless environments (e.g., AWS Lambda, Google Cloud Functions) where the lifecycle of processes is strictly managed or background tasks are restricted, it's recommended to rely on the `precache_fast_counts` management command scheduled externally in such cases.

### Manual Precaching (Management Command)

You can (and should, for reliability) set up a scheduled task (e.g., a cron job) to run the `precache_fast_counts` management command:

```bash
python manage.py precache_fast_counts
```

This command iterates through all models in your project, finds those using `FastCountManager`, and triggers their precaching logic. It also cleans up expired `FastCount` entries from the database.

## Advanced Usage: Subclassing

You can extend `FastCountManager` and `FastCountQuerySet` to add custom logic while retaining the fast counting capabilities.

### Subclassing `FastCountQuerySet`

If you need custom methods on your queryset:

```python
from django_fast_count.managers import FastCountQuerySet

class MyCustomQuerySet(FastCountQuerySet):
    
    def active(self):
        return self.filter(is_active=True)
    
    # If you override __init__, ensure you correctly handle manager_instance
    # and other FastCount settings, typically by passing them to super().
    # If you don't override __init__, the base FastCountQuerySet.__init__
    # will handle configuration from manager_instance automatically.
    #
    # def __init__(self, *args, my_custom_qs_param=None, **kwargs):
    #     # manager_instance should be in kwargs if passed from manager's get_queryset
    #     super().__init__(*args, **kwargs)
    #     self.my_custom_qs_param = my_custom_qs_param
```

### Subclassing `FastCountManager`

To use your custom queryset, your custom manager must override `get_queryset()`:

```python
from django_fast_count.managers import FastCountManager

# from .querysets import MyCustomQuerySet # Assuming MyCustomQuerySet is in querysets.py
class MyCustomManager(FastCountManager):
    def get_queryset(self):
        # Critical: Instantiate your custom queryset.
        # Pass `manager_instance=self` to ensure it's correctly configured
        # with model, db, and all FastCountManager settings.
        return MyCustomQuerySet(self.model, using=self._db, manager_instance=self)
    
    # Example of a custom manager method using the custom queryset
    def get_active_count(self):
        return self.get_queryset().active().count()
    
    # If you override __init__, call super and handle your custom params.
    # def __init__(self, *args, my_custom_mgr_param=None, **kwargs):
    #     super().__init__(*args, **kwargs) # Passes FC params like precache_count_every
    #     self.my_custom_mgr_param = my_custom_mgr_param
```

### Example: Using Custom Manager and QuerySet

```python
# models.py
from django.db import models

# from .managers import MyCustomManager # Assuming MyCustomManager is in managers.py
class Product(models.Model):
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    stock = models.PositiveIntegerField(default=0)
    
    objects = MyCustomManager() # Use your custom manager
    # fast_count_querysets can still be used with custom managers
    
    @classmethod
    def fast_count_querysets(cls):
        return [
            cls.objects.active(), # Uses MyCustomQuerySet.active()
            cls.objects.filter(stock__gt=0),
        ]
    
# Usage:
# Product.objects.count() # Uses fast count
# Product.objects.active().count() # Uses fast count for the filtered active products
# Product.objects.get_active_count() # Uses fast count via custom manager method
```

**Key for Subclassing `get_queryset()`**:

When overriding `get_queryset` in your `FastCountManager` subclass, ensure you instantiate your custom `FastCountQuerySet` subclass and pass `manager_instance=self` to its constructor. The `FastCountQuerySet` base class's `__init__` method uses `manager_instance` to correctly set up the `model`, database connection (`using`), and all fast-count configuration parameters (like `precache_count_every`, `cache_counts_larger_than`, etc.) that were defined on the manager instance.

## Configuration Reference

### `FastCountManager` Initialization Parameters:

*   `precache_count_every`: `timedelta`, default `timedelta(minutes=10)`.
*   `cache_counts_larger_than`: `int`, default `1,000,000`.
*   `expire_cached_counts_after`: `timedelta`, default `timedelta(minutes=10)`.
*   `precache_lock_timeout`: `timedelta` or `int` (seconds), default: `max(300, precache_count_every_seconds * 1.5)`.

### Model `fast_count_querysets(cls)` Method:

*   Optional `classmethod` on your model.
*   Returns a list of `QuerySet` instances to be precached.
*   If not provided, only `YourModel.objects.all()` is precached.

### Environment Variables:

*   `DJANGO_FAST_COUNT_FORCE_SYNC_PRECACHE`: Set to `1` or `true` to run precaching synchronously in the current process when triggered by `maybe_trigger_precache()`, instead of launching a background subprocess. This is useful for testing or environments where background subprocesses are problematic.
*   `DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING` (Legacy): This older environment variable is also recognized for backward compatibility and has the same effect as `DJANGO_FAST_COUNT_FORCE_SYNC_PRECACHE`.

## Management Commands

### `precache_fast_counts`

```bash
python manage.py precache_fast_counts
```
 
* Iterates through all registered Django models.
*   Identifies models using `FastCountManager` (or its subclasses).
*   For each identified manager, calls its `precache_counts()` logic. This involves:
    *   Getting the querysets from the model's `fast_count_querysets()` method (plus the default `.all()`).
    *   Executing `.count()` for each of these querysets.
    *   Storing the results in the `FastCount` database table and Django's cache.
*   Deletes any expired `FastCount` entries from the database.
*   It is recommended to run this command regularly via a scheduler (e.g., cron).

## Considerations & Limitations

*   **Stale Counts**: Cached counts can become stale between updates. The `expire_cached_counts_after` and `precache_count_every` settings control this trade-off between accuracy and performance.
*   **Serverless Environments**: The automatic background subprocess for precaching might not work reliably in serverless environments. Rely on the `precache_fast_counts` management command scheduled externally (e.g., AWS EventBridge, Google Cloud Scheduler).
*   **Complex Queries**: While `django-fast-count` aims to support most querysets, extremely complex or unusual query structures might have unforeseen interactions. Test thoroughly.
*   **Database Backend**: Designed to be database-agnostic, but performance characteristics of `COUNT(*)` can vary between databases. This package primarily addresses the overhead of Django's default counting for very large tables.

## Contributing
Contributions are welcome! Please see `CONTRIBUTING.md` for guidelines.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.