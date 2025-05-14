import pytest
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock, ANY
from io import StringIO
import os
import time  # For time.time mocking
import builtins  # For patching getattr
from testapp.models import TestModel
from django_fast_count.models import FastCount
from django_fast_count.managers import FastCountModelManager, FastCountQuerySet, DISABLE_FORK_ENV_VAR
from django.db import models as django_models
from django.apps import apps as django_apps  # For patching get_models

# Pytest marker for DB access
pytestmark = pytest.mark.django_db

@pytest.fixture(autouse=True)
def clear_state_and_env():
    """Ensures a clean state for each test."""
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    original_fork_setting = os.environ.pop(DISABLE_FORK_ENV_VAR, None)
    yield
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    if original_fork_setting is not None:
        os.environ[DISABLE_FORK_ENV_VAR] = original_fork_setting
    elif DISABLE_FORK_ENV_VAR in os.environ:
        del os.environ[DISABLE_FORK_ENV_VAR]

def create_test_models(count=1, flag=True):
    TestModel.objects.bulk_create([TestModel(flag=flag) for _ in range(count)])

# --- Tests for src/django_fast_count/management/commands/precache_fast_counts.py ---

# Define a sacrificial model for the manager discovery fallback test
class FallbackDiscoveryTestModel(django_models.Model):
    objects = django_models.Manager() # Will be replaced

    class Meta:
        app_label = "testapp_fb_discover"
        managed = False

def test_precache_command_manager_discovery_fallback(capsys, monkeypatch):
    """
    Covers line 21 in precache_fast_counts.py:
    Fallback manager discovery: `if not managers and hasattr(model, "objects")`
    """
    # Use the sacrificial FallbackDiscoveryTestModel to avoid altering TestModel._meta
    # and causing teardown issues.

    # 1. Create a FastCountModelManager instance and set its model
    mock_objects_manager = FastCountModelManager()
    monkeypatch.setattr(mock_objects_manager, "model", FallbackDiscoveryTestModel)

    # 2. Monkeypatch FallbackDiscoveryTestModel.objects *before* altering its _meta.managers_map.
    #    This allows monkeypatch to correctly save the original descriptor.
    #    The ManagerDescriptor.__get__ for FallbackDiscoveryTestModel.objects needs a valid
    #    managers_map at the time of this setattr to retrieve the original value.
    monkeypatch.setattr(FallbackDiscoveryTestModel, "objects", mock_objects_manager)

    # 3. Now, set FallbackDiscoveryTestModel._meta.managers_map to {} to trigger the fallback
    #    logic in the command. monkeypatch will ensure this is reverted after the test.
    monkeypatch.setattr(FallbackDiscoveryTestModel._meta, "managers_map", {})

    ContentType.objects.get_for_model(FallbackDiscoveryTestModel)  # Ensure CT type exists

    with patch("django.apps.apps.get_models", return_value=[FallbackDiscoveryTestModel]):
        call_command("precache_fast_counts")

    captured = capsys.readouterr()
    assert f"Processing: {FallbackDiscoveryTestModel._meta.app_label}.{FallbackDiscoveryTestModel.__name__} (manager: 'objects')" in captured.out


def test_precache_command_general_error_in_manager_processing(capsys, monkeypatch):
    """
    Covers lines 45-46 in precache_fast_counts.py:
    General error during `manager_instance.precache_counts()`.
    """
    create_test_models(1)
    mock_manager_precache = MagicMock(side_effect=Exception("Global Precache Kaboom!"))
    monkeypatch.setattr(TestModel.objects, "precache_counts", mock_manager_precache)

    stderr_capture = StringIO()
    call_command("precache_fast_counts", stdout=StringIO(), stderr=stderr_capture)  # Capture stderr
    err_output = stderr_capture.getvalue()
    assert f"Error precaching for {TestModel._meta.app_label}.{TestModel.__name__} ('objects'): Global Precache Kaboom!" in err_output

# --- Tests for src/django_fast_count/managers.py ---

def test_fcqs_get_manager_name_no_manager_or_model_attr(capsys):
    """
    Covers line 44 in managers.py (FastCountQuerySet._get_manager_name):
    Fallback print when self.manager is None or has no 'model' attribute.
    """
    # Ensure manager can be retrieved for TestModel for other parts of the test if necessary
    # This also ensures ContentType for TestModel is created.
    _ = TestModel.objects

    qs_no_manager = FastCountQuerySet(model=TestModel)
    # Case 1: qs.manager is None (default after FastCountQuerySet(model=TestModel))
    manager_name_1 = qs_no_manager._get_manager_name()
    assert manager_name_1 == "objects"
    captured_1 = capsys.readouterr()
    assert f"Warning: Could not determine manager name for {TestModel.__name__}. Falling back to 'objects'." in captured_1.out

    # Case 2: qs.manager exists but has no 'model' attribute
    qs_manager_no_model = FastCountQuerySet(model=TestModel)
    # Create a mock manager that doesn't have a 'model' attribute when checked by hasattr
    mock_manager_without_model = MagicMock(spec=FastCountModelManager)  # Use spec for isinstance checks
    # To truly simulate no 'model' attribute for hasattr, we ensure it's not present.
    if hasattr(mock_manager_without_model, "model"):
        del mock_manager_without_model.model  # Ensure 'model' attribute is missing
    qs_manager_no_model.manager = mock_manager_without_model

    manager_name_2 = qs_manager_no_model._get_manager_name()
    assert manager_name_2 == "objects"
    captured_2 = capsys.readouterr()
    assert f"Warning: Could not determine manager name for {TestModel.__name__}. Falling back to 'objects'." in captured_2.out


def test_fcqs_count_db_cache_generic_error(monkeypatch, capsys):
    """
    Covers line 93 in managers.py (FastCountQuerySet.count):
    Error print when FastCount.objects.get() raises a generic Exception.
    (Old line number was ~69)
    """
    create_test_models(5)  # Actual count is 5
    monkeypatch.setattr(TestModel.objects, "maybe_trigger_precache", lambda *args, **kwargs: None)
    cache_key = TestModel.objects._get_cache_key(TestModel.objects.all())
    cache.delete(cache_key)

    original_qs_get = django_models.query.QuerySet.get
    def new_qs_get(qs_self, *args, **kwargs):
        if qs_self.model == FastCount: # Check if this QuerySet.get is for the FastCount model
            raise Exception("DB Cache Read Error (patched)")
        return original_qs_get(qs_self, *args, **kwargs) # Call original for other models

    with patch("django.db.models.query.QuerySet.get", new=new_qs_get):
        assert TestModel.objects.count() == 5  # Should fall back to actual DB count

    captured = capsys.readouterr()
    assert f"Error checking FastCount DB cache for {TestModel.__name__} ({cache_key}): DB Cache Read Error (patched)" in captured.out

def test_fcqs_count_retroactive_cache_db_error(monkeypatch, capsys):
    """
    Covers lines 125-126 in managers.py (FastCountQuerySet.count):
    Error print when FastCount.objects.update_or_create() for retroactive cache fails.
    (Old line numbers were ~106-109)
    """
    create_test_models(10)  # Actual count 10
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 5)
    monkeypatch.setattr(TestModel.objects, "maybe_trigger_precache", lambda *args, **kwargs: None)
    cache_key = TestModel.objects._get_cache_key(TestModel.objects.all())
    cache.delete(cache_key)
    FastCount.objects.filter(queryset_hash=cache_key).delete()

    original_qs_uoc = django_models.query.QuerySet.update_or_create
    def new_qs_uoc(qs_self, *args, **kwargs):
        if qs_self.model == FastCount: # Check if this QuerySet.uoc is for the FastCount model
            # Make sure this is the specific update_or_create we want to fail
            # For this test, any uoc on FastCount model is fine to fail
            raise Exception("DB Retro Cache Write Error (patched)")
        return original_qs_uoc(qs_self, *args, **kwargs)

    with patch("django.db.models.query.QuerySet.update_or_create", new=new_qs_uoc):
        assert TestModel.objects.count() == 10

    captured = capsys.readouterr()
    assert f"Error retroactively caching count in DB for {TestModel.__name__} ({cache_key}): DB Retro Cache Write Error (patched)" in captured.out
    assert not FastCount.objects.filter(queryset_hash=cache_key).exists()


def test_fcmanager_init_precache_lock_timeout_types():
    """
    Covers lines 140-145 in managers.py (FastCountModelManager.__init__):
    Initialization with timedelta and int for precache_lock_timeout.
    (Old line numbers were ~138-140)
    """
    manager_td = FastCountModelManager(precache_lock_timeout=timedelta(seconds=120))
    assert manager_td.precache_lock_timeout == 120

    manager_int = FastCountModelManager(precache_lock_timeout=180)
    assert manager_int.precache_lock_timeout == 180

    # Test default calculation (precache_lock_timeout=None)
    manager_default_short_every = FastCountModelManager(precache_count_every=timedelta(minutes=2))  # 120s
    # Expected: max(300, 120 * 1.5) = max(300, 180) = 300
    assert manager_default_short_every.precache_lock_timeout == 300

    manager_default_long_every = FastCountModelManager(precache_count_every=timedelta(minutes=60))  # 3600s
    # Expected: max(300, 3600 * 1.5) = max(300, 5400) = 5400
    assert manager_default_long_every.precache_lock_timeout == 5400


class ModelWithOtherTypeErrorInFCQ(django_models.Model):
    objects = FastCountModelManager()

    @classmethod
    def fast_count_querysets(cls):
        # This will raise a TypeError, but not the one about missing args
        return sum(["not", "a", "list", "of", "querysets"])  # type: ignore

    class Meta:
        app_label = "testapp_covimp_other_typeerror"
        managed = False

def test_fcmanager_get_precache_querysets_other_typeerror(capsys):
    """
    Covers lines 220-221 in managers.py (get_precache_querysets):
    Error print for a TypeError from fast_count_querysets not matching "missing 1 required".
    (Old line numbers were ~174-175)
    """
    manager = ModelWithOtherTypeErrorInFCQ.objects
    querysets = manager.get_precache_querysets()
    assert len(querysets) == 1
    assert querysets[0].model == ModelWithOtherTypeErrorInFCQ
    assert not querysets[0].query.where  # .all()

    captured = capsys.readouterr()
    assert f"Error calling fast_count_querysets for {ModelWithOtherTypeErrorInFCQ.__name__}" in captured.out
    assert ("unsupported operand type(s)" in captured.out or
            "can only concatenate str (not \"int\") to str" in captured.out or
            "must be str, not int" in captured.out)
    assert "seems to be an instance method" not in captured.out


@patch("os.fork")
def test_fcmanager_maybe_trigger_precache_fork_oserror(mock_os_fork, monkeypatch, capsys):
    """
    Covers lines 399-401 in managers.py (maybe_trigger_precache):
    Error print when os.fork() raises OSError.
    (Old line numbers were ~258-260)
    """
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    mock_os_fork.side_effect = OSError("Fork failed spectacularly")

    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    cache.set(last_run_key, 0) # Ensure precache logic attempts to run

    manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    captured = capsys.readouterr()
    assert f"Error forking/managing precache process for {model_ct} ({manager_name}): Fork failed spectacularly" in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None


def test_fcmanager_maybe_trigger_precache_outer_exception(monkeypatch, capsys):
    """
    Covers line ~405 in managers.py (maybe_trigger_precache):
    Outer try-except block catches an unexpected error.
    """
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    cache.set(last_run_key, 0)  # Ensure the precache logic attempts to run

    # Make os.environ.get raise an exception *after* the lock is acquired.
    # This error occurs inside the main try block of maybe_trigger_precache.
    with patch("os.environ.get", side_effect=Exception("Environ Get Kaboom")):
        # Ensure cache.add succeeds so we enter the main try block
        with patch("django.core.cache.cache.add", return_value=True) as mock_cache_add:
            manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
            mock_cache_add.assert_called_once()  # Verify lock acquisition was attempted

    captured = capsys.readouterr()
    assert f"Unexpected error during precache trigger for {model_ct} ({manager_name}): Environ Get Kaboom" in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None