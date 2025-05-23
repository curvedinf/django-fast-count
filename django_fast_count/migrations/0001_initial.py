# Generated by Django 5.2 on 2025-05-14 18:02

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="FastCount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "manager_name",
                    models.CharField(
                        db_index=True,
                        help_text="The name of the manager on the model (e.g., 'objects').",
                        max_length=100,
                    ),
                ),
                (
                    "queryset_hash",
                    models.CharField(
                        db_index=True,
                        help_text="MD5 hash representing the specific queryset.",
                        max_length=32,
                    ),
                ),
                ("count", models.BigIntegerField(help_text="The cached count.")),
                (
                    "last_updated",
                    models.DateTimeField(
                        auto_now=True,
                        help_text="When the count was last calculated and cached.",
                    ),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        db_index=True, help_text="When this cached count should expire."
                    ),
                ),
                (
                    "is_precached",
                    models.BooleanField(
                        db_index=True,
                        default=False,
                        help_text="Whether the count was pre-cached or retroactively cached.",
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        help_text="The model for which the count is cached.",
                        on_delete=django.db.models.deletion.CASCADE,
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "verbose_name": "Fast Count Cache Entry",
                "verbose_name_plural": "Fast Count Cache Entries",
                "unique_together": {("content_type", "manager_name", "queryset_hash")},
            },
        ),
    ]
