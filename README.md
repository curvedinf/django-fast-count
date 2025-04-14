from datetime import timedelta

# django-fast-count
Fast `queryset.count()` implementation for large tables.

## Summary

For most databases, when a table begins to exceed several million rows,
the performance of the default `queryset.count()` implementation begins to be 
poor. Sometimes it is so poor that a count is the slowest query in a view by 
several orders of magnitude.

This package provides a fast, plug-and-play, database agnostic count implementation.
The implementation is based on background caching of counts.

## Installation

```bash
pip install django-fast-count
```

```python
# settings.py

INSTALLED_APPS = [
    # ...
    'contenttypes',
    'django_fast_count',
]
```

```bash
python manage.py migrate
```

## Usage

```python
from datetime import timedelta

from django.db.models import Model, BooleanField
from django_fast_count import FastCountModelManager


class YourModel(Model):
    your_field = BooleanField(default=False)

    # By default, only .all() is precached
    objects = FastCountModelManager(
        precache_count_every=timedelta(hours=1), # Defaults to 10 minutes
        cache_counts_larger_than=100_000, # Defaults to 1,000,000
        expire_cached_counts_after=timedelta(hours=1), # Defaults to 10 minutes
    )

    # To cache additional querysets, override the `fast_count_querysets`
    def fast_count_querysets(self):
        return [
            self.objects.filter(your_field=True),
            self.objects.filter(your_field=False),
        ]
```

## FastCountModelManager

The `FastCountModelManager` is a subclass of the default django `ModelManager` that 
overrides `.count()` to use utilize cached counts. It has two main caching mechanisms:

1. Precaching of select `.count()` queries every specified interval
2. Retroactive caching of any `.count()` queries that return a count over a threshold

It has 3 initialization parameters:

1. `precache_count_every` - The frequency at which to precache select `.count()` queries
2. `cache_counts_larger_than` - The minimum count at which to retroactively cache `.count()` queries
3. `expire_cached_counts_after` - The frequency at which to expire cached `.count()` queries

By default, `FastCountModelManager` will only precache `.all()` queries. To specify additional
QuerySets to precache, implement a `fast_count_querysets` method on your model that returns a 
list of QuerySets. Each of those QuerySets will be counted every `precache_count_every` and cached
for use on future matching `.count()` queries.

## Precaching Process

Precaching of counts is performed regularly by a management command that is called from a forked
process. The forked process is started every `precache_count_every` from any `.count()` query
performed on the model.

Typically, this means that precaching is performed in a background task on your web server,
so if your django deploy is serverless, the precaching process may end early and not function
properly.

Deadlock control over the precaching scheduler is implemented with atomic transactions so that
multiple `.count()` queries do not simultaneously run the precaching process.