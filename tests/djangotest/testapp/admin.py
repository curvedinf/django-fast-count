from django.contrib import admin
from .models import TestModel

@admin.register(TestModel)
class TestModelAdmin(admin.ModelAdmin):
    list_display = ("id", "uuid", "flag")
    list_filter = ("flag",)
    search_fields = ("uuid",)
    readonly_fields = ("uuid",)