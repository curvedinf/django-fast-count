import uuid
from datetime import timedelta
from random import choice
from django.db import models
from django_fast_count.managers import FastCountModelManager

def get_random_boolean():
    return choice([True, False])

class TestModel(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4)
    flag = models.BooleanField(default=get_random_boolean)

    objects = FastCountModelManager(
        precache_count_every=timedelta(minutes=1),
        cache_counts_larger_than=1000,
        expire_cached_counts_after=timedelta(minutes=1),
    )

    @classmethod
    def fast_count_querysets(cls):
        return [
            cls.objects.filter(flag=True),
            cls.objects.filter(flag=False),
        ]

class ModelWithBadFastCountQuerysets(models.Model):
    objects = FastCountModelManager()
    @classmethod
    def fast_count_querysets(cls):
        return "not a list or tuple" # Incorrect return type
    class Meta:
        app_label = "testapp"

class ModelWithDynamicallyAssignedManager(models.Model):
    some_field = models.BooleanField(default=True)
    # No explicit manager here, tests might assign it dynamically or test fallback
    class Meta:
        app_label = "testapp"

class AnotherTestModel(models.Model): # Model without FastCountManager
    name = models.CharField(max_length=100)
    objects = models.Manager()
    class Meta:
        app_label = "testapp"

class ModelWithSimpleManager(models.Model): # For manager discovery fallback
    data = models.CharField(max_length=10)
    objects = FastCountModelManager()
    @classmethod
    def fast_count_querysets(cls):
        return [cls.objects.filter(data="test")]
    class Meta:
        app_label = "testapp"