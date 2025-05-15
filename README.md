# Django Fast Count

[![PyPI](https://img.shields.io/pypi/v/django-fast-count)](https://pypi.org/project/django-fast-count/)
[![GitHub license](https://img.shields.io/github/license/curvedinf/django-fast-count)](LICENSE)
[![GitHub last commit](https://img.shields.io/github/last-commit/curvedinf/django-fast-count)](https://github.com/curvedinf/django-fast-count/commits/main)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/django-fast-count)](https://pypi.org/project/django-fast-count/)
[![GitHub stars](https://img.shields.io/github/stars/curvedinf/django-fast-count)](https://github.com/curvedinf/django-fast-count/stargazers)
[![Ko-fi Link](kofi.webp)](https://ko-fi.com/A0A31B6VB6)

A fast [Django](https://djangoproject.com) `.count()` implementation for large tables.

## Summary

For most databases, when a table begins to exceed several million rows,
the performance of the default `QuerySet.count()` implementation begins to be 
poor. Sometimes it is so poor that a count is the slowest query in a view by 
several orders of magnitude. Since the Django admin app uses `.count()` on every
list page, this can be annoying at best or unusable at worst.

This package provides a fast, plug-and-play, database agnostic count 
implementation. To use it, you just need to have 
`django-fast-count` installed and then override your Model's 
`ModelManager` with `FastCountModelManager`.

After `FastCountModelManager` is on your Model, fast counts are immediately
activate. Precaching for all `.count()` queries is triggered automatically
on every `.count()` query in a forked background process.

To proactively precache and clean expired counts, run `precache_fast_counts`
in a regularly scheduled task.

## Installation

```bash
pip install django-fast-count
```

```python
# settings.py

INSTALLED_APPS = [
    # ...
    'django.contrib.contenttypes',
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
from django_fast_count.managers import FastCountModelManager


class YourModel(Model):
    your_field = BooleanField(default=False)

    # By default, only .all() is precached
    objects = FastCountModelManager(
        precache_count_every=timedelta(hours=1),  # Defaults to 10 minutes
        cache_counts_larger_than=100_000,  # Defaults to 1,000,000
        expire_cached_counts_after=timedelta(hours=1),  # Defaults to 10 minutes
    )

    # To cache additional querysets, override the `fast_count_querysets`
    @classmethod
    def fast_count_querysets(cls):
        return [
            cls.objects.filter(your_field=True),
            cls.objects.filter(your_field=False),
        ]
```

## FastCountModelManager

The `FastCountModelManager` is a subclass of the default django `ModelManager` that 
overrides `.count()` to use utilize cached counts. It has two main caching mechanisms:

1. Precaching of select `.count()` queries every specified interval
2. Retroactive caching of any `.count()` queries that return a count over a threshold

It has 3 initialization parameters:

1. `precache_count_every` - The frequency with which to precache select `.count()` queries
2. `cache_counts_larger_than` - The minimum count at which to retroactively cache all other
`.count()` queries
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
