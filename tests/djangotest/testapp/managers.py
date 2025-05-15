from django_fast_count.managers import FastCountModelManager, FastCountQuerySet

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

# Two-deep inheritance for FastCountModelManager
class IntermediateFastCountModelManager(FastCountModelManager):
    """
    An intermediate ModelManager inheriting from FastCountModelManager.
    """
    def get_queryset(self):
        qs = IntermediateFastCountQuerySet(self.model, using=self._db)
        qs.manager = self
        return qs

class DeepFastCountModelManager(IntermediateFastCountModelManager):
    """
    A ModelManager inheriting from IntermediateFastCountModelManager.
    """
    def get_queryset(self):
        qs = DeepFastCountQuerySet(self.model, using=self._db)
        qs.manager = self
        return qs