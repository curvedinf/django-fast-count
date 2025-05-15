import pytest
from django.core.cache import cache
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock
from io import StringIO
import os
import sys
import subprocess
from testapp.models import TestModel
from django_fast_count.models import FastCount
from django_fast_count.managers import (
    FastCountManager,
    FastCountQuerySet,
    FORCE_SYNC_PRECACHE_ENV_VAR,
    _DISABLE_FORK_ENV_VAR_OLD_NAME,
)
from django.db import models as django_models

# Pytest marker for DB access
pytestmark = pytest.mark.django_db

@pytest.fixture(autouse=True)
def clean_state_and_env_and_settings(settings):
    """Ensures a clean state for each test."""
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    original_sync_setting = os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None)
    original_sync_setting_old = os.environ.pop(_DISABLE_FORK_ENV_VAR_OLD_NAME, None)
    original_base_dir = getattr(settings, "BASE_DIR", None)
    # Set a mock BASE_DIR for tests that rely on it for subprocess calls
    settings.BASE_DIR = os.getcwd()
    yield
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    if original_sync_setting is not None:
        os.environ[FORCE_SYNC_PRECACHE_ENV_VAR] = original_sync_setting
    elif FORCE_SYNC_PRECACHE_ENV_VAR in os.environ:
        del os.environ[FORCE_SYNC_PRECACHE_ENV_VAR]
    if original_sync_setting_old is not None:
        os.environ[_DISABLE_FORK_ENV_VAR_OLD_NAME] = original_sync_setting_old
    elif _DISABLE_FORK_ENV_VAR_OLD_NAME in os.environ:
        del os.environ[_DISABLE_FORK_ENV_VAR_OLD_NAME]

    if original_base_dir is not None:
        settings.BASE_DIR = original_base_dir
    elif hasattr(settings, "BASE_DIR"):
        delattr(settings, "BASE_DIR")

def create_test_models(count=1, flag=True):
    TestModel.objects.bulk_create([TestModel(flag=flag) for _ in range(count)])

# --- Tests for src/django_fast_count/management/commands/precache_fast_counts.py ---

# Define a sacrificial model for the manager discovery fallback test
class FallbackDiscoveryTestModel(django_models.Model):
    objects = django_models.Manager()  # Will be replaced
    class Meta:
        app_label = "testapp_fb_discover"
        managed = False

def test_precache_command_manager_discovery_fallback(capsys, monkeypatch):
    """
    Covers fallback manager discovery in precache_fast_counts.py:
    `if not managers and hasattr(model, "objects")`
    """
    # Use the sacrificial FallbackDiscoveryTestModel
    mock_objects_manager = FastCountManager()
    monkeypatch.setattr(mock_objects_manager, "model", FallbackDiscoveryTestModel)
    monkeypatch.setattr(FallbackDiscoveryTestModel, "objects", mock_objects_manager)
    monkeypatch.setattr(FallbackDiscoveryTestModel._meta, "managers_map", {})
    ContentType.objects.get_for_model(
        FallbackDiscoveryTestModel
    )  # Ensure CT type exists

    # Mock FastCountQuerySet.precache_counts as it's called by the command
    with patch(
        "django_fast_count.managers.FastCountQuerySet.precache_counts", return_value={}
    ) as mock_qs_precache:
        with patch(
            "django.apps.apps.get_models", return_value=[FallbackDiscoveryTestModel]
        ):
            call_command("precache_fast_counts")

    captured = capsys.readouterr()
    assert (
        f"Processing: {FallbackDiscoveryTestModel._meta.app_label}.{FallbackDiscoveryTestModel.__name__} (manager: 'objects')"
        in captured.out
    )
    mock_qs_precache.assert_called_once()

def test_precache_command_general_error_in_manager_processing(capsys, monkeypatch):
    """
    Covers general error during `manager_instance.get_queryset().precache_counts()`.
    in precache_fast_counts.py
    """
    create_test_models(1)
    stderr_capture = StringIO()
    # Patch FastCountQuerySet.precache_counts to raise an error
    with patch(
        "django_fast_count.managers.FastCountQuerySet.precache_counts",
        side_effect=Exception("Global Precache Kaboom!"),
    ) as mock_qs_precache:
        call_command("precache_fast_counts", stdout=StringIO(), stderr=stderr_capture)

    err_output = stderr_capture.getvalue()
    assert (
        f"Error precaching for {TestModel._meta.app_label}.{TestModel.__name__} ('objects'): Global Precache Kaboom!"
        in err_output
    )
    # Ensure it was TestModel's manager ('objects') that triggered this
    # The command calls get_queryset() on TestModel.objects, then precache_counts()
    # So mock_qs_precache should have been called.
    assert mock_qs_precache.called

# --- Tests for src/django_fast_count/managers.py ---

def test_fcqs_count_db_cache_generic_error(monkeypatch, capsys):
    """
    Covers error print in FastCountQuerySet.count()
    when FastCount.objects.get() raises a generic Exception.
    """
    create_test_models(5)  # Actual count is 5
    qs = TestModel.objects.all()

    # Patch FastCountQuerySet.maybe_trigger_precache to do nothing for this test
    monkeypatch.setattr(
        FastCountQuerySet, "maybe_trigger_precache", lambda *args, **kwargs: None
    )

    cache_key = qs._get_cache_key()
    cache.delete(cache_key)

    original_qs_get = django_models.query.QuerySet.get

    def new_qs_get(qs_self, *args, **kwargs):
        if (
            qs_self.model == FastCount
        ):  # Check if this QuerySet.get is for the FastCount model
            raise Exception("DB Cache Read Error (patched)")
        return original_qs_get(
            qs_self, *args, **kwargs
        )  # Call original for other models

    with patch("django.db.models.query.QuerySet.get", new=new_qs_get):
        assert qs.count() == 5  # Should fall back to actual DB count

    captured = capsys.readouterr()
    # The error message now includes manager_name
    assert (
        f"Error checking FastCount DB cache for {TestModel.__name__} ({qs.manager_name}, {cache_key}): DB Cache Read Error (patched)"
        in captured.out
    )

def test_fcqs_count_retroactive_cache_db_error(monkeypatch, capsys):
    """
    Covers error print in FastCountQuerySet.count()
    when FastCount.objects.update_or_create() for retroactive cache fails.
    """
    create_test_models(10)  # Actual count 10
    # Configure manager for this test scenario
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 5)
    qs = TestModel.objects.all()

    # Patch FastCountQuerySet.maybe_trigger_precache to do nothing for this test
    monkeypatch.setattr(
        FastCountQuerySet, "maybe_trigger_precache", lambda *args, **kwargs: None
    )

    cache_key = qs._get_cache_key()
    cache.delete(cache_key)
    FastCount.objects.filter(queryset_hash=cache_key).delete()

    original_qs_uoc = django_models.query.QuerySet.update_or_create

    def new_qs_uoc(qs_self, *args, **kwargs):
        if qs_self.model == FastCount:
            raise Exception("DB Retro Cache Write Error (patched)")
        return original_qs_uoc(qs_self, *args, **kwargs)

    with patch("django.db.models.query.QuerySet.update_or_create", new=new_qs_uoc):
        assert qs.count() == 10

    captured = capsys.readouterr()
    # The error message now includes manager_name
    assert (
        f"Error retroactively caching count in DB for {TestModel.__name__} ({qs.manager_name}, {cache_key}): DB Retro Cache Write Error (patched)"
        in captured.out
    )
    assert not FastCount.objects.filter(queryset_hash=cache_key).exists()

def test_fcmanager_init_precache_lock_timeout_types():
    """
    Covers FastCountManager.__init__ logic for precache_lock_timeout.
    """
    manager_td = FastCountManager(precache_lock_timeout=timedelta(seconds=120))
    assert manager_td.precache_lock_timeout == 120

    manager_int = FastCountManager(precache_lock_timeout=180)
    assert manager_int.precache_lock_timeout == 180

    # Test default calculation (precache_lock_timeout=None)
    manager_default_short_every = FastCountManager(
        precache_count_every=timedelta(minutes=2)
    )  # 120s
    # Expected: max(300, 120 * 1.5) = max(300, 180) = 300
    assert manager_default_short_every.precache_lock_timeout == 300

    manager_default_long_every = FastCountManager(
        precache_count_every=timedelta(minutes=60)
    )  # 3600s
    # Expected: max(300, 3600 * 1.5) = max(300, 5400) = 5400
    assert manager_default_long_every.precache_lock_timeout == 5400


class ModelWithOtherTypeErrorInFCQ(django_models.Model):
    objects = FastCountManager()

    @classmethod
    def fast_count_querysets(cls):
        # This will raise a TypeError, but not the one about missing args
        return sum(["not", "a", "list", "of", "querysets"])  # type: ignore

    class Meta:
        app_label = "testapp_covimp_other_typeerror"
        managed = False


def test_fcqs_get_precache_querysets_other_typeerror(capsys):
    """
    Covers error print in FastCountQuerySet.get_precache_querysets()
    for a TypeError from model's fast_count_querysets not matching "missing 1 required".
    """
    qs_instance = ModelWithOtherTypeErrorInFCQ.objects.all()
    querysets = qs_instance.get_precache_querysets()

    assert len(querysets) == 1  # Should fallback to .all()
    assert querysets[0].model == ModelWithOtherTypeErrorInFCQ
    assert not querysets[0].query.where  # .all()

    captured = capsys.readouterr()
    assert (
        f"Error calling or processing fast_count_querysets for {ModelWithOtherTypeErrorInFCQ.__name__}"
        in captured.out
    )
    # sum() on list of strings raises TypeError: unsupported operand type(s) for +: 'int' and 'str'
    assert (
        "unsupported operand type(s)" in captured.out
        or 'can only concatenate str (not "int") to str' in captured.out  # older python
        or "must be str, not int" in captured.out  # newer python sum behavior
    )
    assert "seems to be an instance method" not in captured.out


@patch("subprocess.Popen")
def test_fcqs_maybe_trigger_precache_subprocess_launch_oserror(mock_subprocess_popen, monkeypatch, capsys, settings):
    """
    Covers error print in FastCountQuerySet.maybe_trigger_precache()
    when subprocess.Popen() raises OSError.
    """
    # settings.BASE_DIR is set by the fixture
    os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None) # Ensure not sync mode
    os.environ.pop(_DISABLE_FORK_ENV_VAR_OLD_NAME, None) # Ensure old var not sync mode

    mock_subprocess_popen.side_effect = OSError("Subprocess launch OSError")

    # Configure manager and get QS instance
    manager = TestModel.objects
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    qs = manager.all()
    model_name = qs.model.__name__

    # Ensure precache logic attempts to run
    model_ct = ContentType.objects.get_for_model(qs.model)
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=qs.manager_name
    )
    cache.set(last_run_key, 0) # Mark as due

    qs.maybe_trigger_precache()
    captured = capsys.readouterr()
    assert (
        f"Error launching background precache command for {model_name} ({qs.manager_name}): Subprocess launch OSError"
        in captured.out
    )
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=qs.manager_name
    )
    assert cache.get(lock_key) is None # Lock should be released


def test_fcqs_maybe_trigger_precache_outer_exception(monkeypatch, capsys, settings):
    """
    Covers outer try-except block in FastCountQuerySet.maybe_trigger_precache()
    catching an unexpected error that is not a Popen launch error.
    """
    # settings.BASE_DIR is set by the fixture
    os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None) # Ensure not sync mode
    os.environ.pop(_DISABLE_FORK_ENV_VAR_OLD_NAME, None) # Ensure old var not sync mode

    manager = TestModel.objects
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    qs = manager.all()
    model_name = qs.model.__name__
    model_ct = ContentType.objects.get_for_model(qs.model)
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=qs.manager_name
    )
    cache.set(last_run_key, 0)  # Ensure the precache logic attempts to run

    # Make os.path.join raise an exception after the lock is acquired and sync mode check is passed.
    with patch("os.path.join", side_effect=Exception("Path Join Kaboom")):
        # Ensure cache.add succeeds so we enter the main try block
        with patch("django.core.cache.cache.add", return_value=True) as mock_cache_add:
            qs.maybe_trigger_precache()
            mock_cache_add.assert_called_once()  # Verify lock acquisition was attempted

    captured = capsys.readouterr()
    # This error should be caught by the outer `except Exception as e:`
    assert (
        f"Unexpected error during precache trigger for {model_name} ({qs.manager_name}): Path Join Kaboom"
        in captured.out
    )
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=qs.manager_name
    )
    assert cache.get(lock_key) is None # Lock should be released
