from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class FastCount(models.Model):
    """
    Stores cached counts for specific model querysets.
    """
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        help_text="The model for which the count is cached.",
    )
    manager_name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="The name of the manager on the model (e.g., 'objects').",
    )
    queryset_hash = models.CharField(
        max_length=32, # MD5 hash length
        db_index=True,
        help_text="MD5 hash representing the specific queryset.",
    )
    count = models.BigIntegerField(
        help_text="The cached count.",
    )
    last_updated = models.DateTimeField(
        auto_now=True,
        help_text="When the count was last calculated and cached.",
    )
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="When this cached count should expire.",
    )
    is_precached = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether the count was pre-cached or retroactively cached.",
    )

    class Meta:
        # Ensure uniqueness for a given model, manager, and queryset hash
        unique_together = ("content_type", "manager_name", "queryset_hash")
        verbose_name = "Fast Count Cache Entry"
        verbose_name_plural = "Fast Count Cache Entries"

    def __str__(self):
        return f"{self.content_type} ({self.manager_name}) [{self.queryset_hash[:8]}...]"