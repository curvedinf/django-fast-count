# Generated by Django 5.2 on 2025-05-14 18:16

import testapp.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("testapp", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="testmodel",
            name="flag",
            field=models.BooleanField(default=testapp.models.get_random_boolean),
        ),
    ]
