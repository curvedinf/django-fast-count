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
    An intermediate ModelManager inheriting from FastCountManager.
    """
    def get_queryset(self):
        qs = IntermediateFastCountQuerySet(self.model, using=self._db)
        qs.manager = self
        return qs

class DeepFastCountManager(IntermediateFastCountManager):
    """
    A ModelManager inheriting from IntermediateFastCountManager.
    """
    def get_queryset(self):
        qs = DeepFastCountQuerySet(self.model, using=self._db)
        qs.manager = self
        return qs