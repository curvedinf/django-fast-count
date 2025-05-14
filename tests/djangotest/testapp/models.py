import uuid
from datetime import timedelta
from random import choice

from django.db.models import Model, UUIDField, BooleanField

from django_fast_count.managers import FastCountModelManager

def get_random_boolean():
    return choice([True, False])

class TestModel(Model):
    uuid = UUIDField(default=uuid.uuid4)
    flag = BooleanField(default=get_random_boolean)

    objects = FastCountModelManager(
        precache_count_every=timedelta(minutes=1),
        cache_counts_larger_than=1_000,
        expire_cached_counts_after=timedelta(minutes=1),
    )

    @classmethod
    def fast_count_querysets(cls):
        return [
            cls.objects.filter(flag=True),
            cls.objects.filter(flag=False),
        ]