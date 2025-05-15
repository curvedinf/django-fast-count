from django_fast_count.managers import FastCountManager, FastCountQuerySet

# Two-deep inheritance for FastCountQuerySet
class IntermediateFastCountQuerySet(FastCountQuerySet):
    """
    An intermediate QuerySet inheriting from FastCountQuerySet.
    """
    pass

class DeepFastCountQuerySet(IntermediateFastCountQuerySet):
    """
    A QuerySet inheriting from IntermediateFastCountQuerySet.
    """
    pass

# Two-deep inheritance for FastCountManager
class IntermediateFastCountManager(FastCountManager):
    """
    An intermediate Manager inheriting from FastCountManager.
    """
    def get_queryset(self):
        return IntermediateFastCountQuerySet(self.model, using=self._db)

class DeepFastCountManager(IntermediateFastCountManager):
    """
    A Manager inheriting from IntermediateFastCountManager.
    """
    def get_queryset(self):
        return DeepFastCountQuerySet(self.model, using=self._db)