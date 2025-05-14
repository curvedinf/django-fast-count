import pytest
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock, ANY
from io import StringIO
import os
import time # For time.time mocking
import builtins # For patching getattr

from testapp.models import TestModel
from django_fast_count.models import FastCount
from django_fast_count.managers import FastCountModelManager, FastCountQuerySet, DISABLE_FORK_ENV_VAR
from django.db import models as django_models
from django.apps import apps as django_apps # For patching get_models

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

def test_precache_command_manager_discovery_fallback(capsys, monkeypatch):
    """
    Covers line 21 in precache_fast_counts.py:
    Fallback manager discovery: `if not managers and hasattr(model, "objects")`
    """
    # Temporarily replace TestModel._meta.managers_map with an empty dict
    # The command line is: managers = getattr(model._meta, "managers_map", {})
    # So, if managers_map is set to {}, then `managers` becomes {}.
    # Then `if not managers` (i.e. `if not {}`) is True.
    original_managers_map = TestModel._meta.managers_map
    monkeypatch.setattr(TestModel._meta, "managers_map", {})
    
    ContentType.objects.get_for_model(TestModel) # Ensure CT type exists for TestModel
    
    with patch("django.apps.apps.get_models", return_value=[TestModel]):
        call_command("precache_fast_counts")

    captured = capsys.readouterr()
    
    assert f"Processing: {TestModel._meta.app_label}.{TestModel.__name__} (manager: 'objects')" in captured.out
    
    monkeypatch.setattr(TestModel._meta, "managers_map", original_managers_map)


def test_precache_command_general_error_in_manager_processing(capsys, monkeypatch):
    """
    Covers lines 45-46 in precache_fast_counts.py:
    General error during `manager_instance.precache_counts()`.
    """
    create_test_models(1)
    
    mock_manager_precache = MagicMock(side_effect=Exception("Global Precache Kaboom!"))
    monkeypatch.setattr(TestModel.objects, "precache_counts", mock_manager_precache)

    stderr_capture = StringIO()
    call_command("precache_fast_counts", stdout=StringIO(), stderr=stderr_capture) # Capture stderr
    
    err_output = stderr_capture.getvalue()
    assert f"Error precaching for {TestModel._meta.app_label}.{TestModel.__name__} ('objects'): Global Precache Kaboom!" in err_output


# --- Tests for src/django_fast_count/managers.py ---

def test_fcqs_get_manager_name_no_manager_or_model_attr(capsys):
    """
    Covers line 44 in managers.py (FastCountQuerySet._get_manager_name):
    Fallback print when self.manager is None or has no 'model' attribute.
    """
    qs_no_manager = FastCountQuerySet(model=TestModel)
    # Case 1: qs.manager is None (default after FastCountQuerySet(model=TestModel))
    manager_name_1 = qs_no_manager._get_manager_name()
    assert manager_name_1 == "objects"
    captured_1 = capsys.readouterr()
    assert f"Warning: Could not determine manager name for {TestModel.__name__}. Falling back to 'objects'." in captured_1.out

    # Case 2: qs.manager exists but has no 'model' attribute
    qs_manager_no_model = FastCountQuerySet(model=TestModel)
    # Create a mock manager that doesn't have a 'model' attribute when checked by hasattr
    mock_manager_without_model = MagicMock(spec=FastCountModelManager) # Use spec for isinstance checks
    del mock_manager_without_model.model # Ensure 'model' attribute is missing
    qs_manager_no_model.manager = mock_manager_without_model
        
    manager_name_2 = qs_manager_no_model._get_manager_name()
    assert manager_name_2 == "objects"
    captured_2 = capsys.readouterr()
    assert f"Warning: Could not determine manager name for {TestModel.__name__}. Falling back to 'objects'." in captured_2.out


def test_fcqs_count_db_cache_generic_error(monkeypatch, capsys):
    """
    Covers line 69 in managers.py (FastCountQuerySet.count):
    Error print when FastCount.objects.get() raises a generic Exception.
    """
    create_test_models(5) # Actual count is 5
    
    cache_key = TestModel.objects._get_cache_key(TestModel.objects.all())
    cache.delete(cache_key) # Ensure Django cache is empty

    with patch("django_fast_count.models.FastCount.objects.get", side_effect=Exception("DB Cache Read Error")):
        assert TestModel.objects.count() == 5 # Should fall back to actual DB count
    
    captured = capsys.readouterr()
    assert f"Error checking FastCount DB cache for {TestModel.__name__} ({cache_key}): DB Cache Read Error" in captured.out


def test_fcqs_count_retroactive_cache_db_error(monkeypatch, capsys):
    """
    Covers lines 106-109 in managers.py (FastCountQuerySet.count):
    Error print when FastCount.objects.update_or_create() for retroactive cache fails.
    """
    create_test_models(10) # Actual count 10
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 5) # Trigger retroactive cache
    
    cache_key = TestModel.objects._get_cache_key(TestModel.objects.all())
    cache.delete(cache_key)
    FastCount.objects.filter(queryset_hash=cache_key).delete()

    with patch("django_fast_count.models.FastCount.objects.update_or_create", side_effect=Exception("DB Retro Cache Write Error")):
        assert TestModel.objects.count() == 10 # Count should still return actual
        
    captured = capsys.readouterr()
    assert f"Error retroactively caching count in DB for {TestModel.__name__} ({cache_key}): DB Retro Cache Write Error" in captured.out
    assert not FastCount.objects.filter(queryset_hash=cache_key).exists() # No DB entry


def test_fcmanager_init_precache_lock_timeout_types():
    """
    Covers lines 138-140 in managers.py (FastCountModelManager.__init__):
    Initialization with timedelta and int for precache_lock_timeout.
    """
    manager_td = FastCountModelManager(precache_lock_timeout=timedelta(seconds=120))
    assert manager_td.precache_lock_timeout == 120

    manager_int = FastCountModelManager(precache_lock_timeout=180)
    assert manager_int.precache_lock_timeout == 180
    
    manager_default = FastCountModelManager(precache_count_every=timedelta(minutes=2))
    assert manager_default.precache_lock_timeout == 300 # max(300, 120*1.5=180)

    manager_default_long = FastCountModelManager(precache_count_every=timedelta(minutes=60))
    assert manager_default_long.precache_lock_timeout == 5400 # max(300, 3600*1.5=5400)


class ModelWithOtherTypeErrorInFCQ(django_models.Model):
    objects = FastCountModelManager()
    @classmethod
    def fast_count_querysets(cls):
        # This will raise a TypeError, but not the one about missing args
        return sum(["not", "a", "list", "of", "querysets"]) # type: ignore
    class Meta:
        app_label = "testapp_covimp_other_typeerror"
        managed = False 

def test_fcmanager_get_precache_querysets_other_typeerror(capsys):
    """
    Covers lines 174-175 in managers.py (get_precache_querysets):
    Error print for a TypeError from fast_count_querysets not matching "missing 1 required".
    """
    manager = ModelWithOtherTypeErrorInFCQ.objects
    querysets = manager.get_precache_querysets()
    
    assert len(querysets) == 1
    assert querysets[0].model == ModelWithOtherTypeErrorInFCQ
    assert not querysets[0].query.where # .all()
    
    captured = capsys.readouterr()
    assert f"Error calling fast_count_querysets for {ModelWithOtherTypeErrorInFCQ.__name__}" in captured.out
    # Check for part of the sum() TypeError message
    assert ("unsupported operand type(s)" in captured.out or 
            "can only concatenate str (not \"int\") to str" in captured.out or
            "must be str, not int" in captured.out) # Python version differences
    assert "seems to be an instance method" not in captured.out


@patch("os.fork")
def test_fcmanager_maybe_trigger_precache_fork_oserror(mock_os_fork, monkeypatch, capsys):
    """
    Covers lines 258-260 in managers.py (maybe_trigger_precache):
    Error print when os.fork() raises OSError.
    """
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    
    mock_os_fork.side_effect = OSError("Fork failed spectacularly")
    
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    cache.set(last_run_key, 0) 

    manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    
    captured = capsys.readouterr()
    assert f"Error forking/managing precache process for {model_ct} ({manager_name}): Fork failed spectacularly" in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None


def test_fcmanager_maybe_trigger_precache_outer_exception(monkeypatch, capsys):
    """
    Covers line 338 in managers.py (maybe_trigger_precache):
    Outer try-except block catches an unexpected error during setup.
    """
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    
    # Case 1: Error in cache.get for last_run_key
    with patch("django.core.cache.cache.get", side_effect=Exception("Cache Read Kaboom")):
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    captured = capsys.readouterr()
    assert f"Unexpected error during precache trigger for {model_ct} ({manager_name}): Cache Read Kaboom" in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None

    # Case 2: Error after lock acquired, before fork/sync call (e.g., time.time fails)
    cache.clear() # Reset for this sub-test
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    cache.set(last_run_key, 0) # Expired

    # Patch os.environ.get to simulate sync mode, then make time.time fail
    # Sync mode is chosen to avoid mocking os.fork issues here.
    with patch("os.environ.get") as mock_os_env_get:
        mock_os_env_get.return_value = "1" # DJANGO_FAST_COUNT_DISABLE_FORK_FOR_TESTING = "1"
        # The error will occur when self.precache_counts (sync) is called,
        # or if time.time() is called again inside that path before precache_counts.
        # To make it fail in the outer "maybe_trigger_precache" before precache_counts,
        # we need an earlier point.
        # The existing `time.time()` calls are for `now_ts` and inside `cache.set(last_run_key, time.time(), None)`
        # Let's mock `cache.add` to error out after successfully setting the lock, to make it interesting.
    
    cache.clear()
    cache.set(last_run_key, 0)
    original_cache_add = cache.add
    def faulty_cache_add(key, value, timeout):
        if key == manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name):
            original_cache_add(key, value, timeout) # Let it acquire the lock
            raise Exception("Faulty Add Post-Acquire") # Then error
        return original_cache_add(key, value, timeout)

    with patch("django.core.cache.cache.add", side_effect=faulty_cache_add):
         with patch("os.environ.get", return_value="1"): # Sync mode
            manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    captured = capsys.readouterr()
    assert f"Unexpected error during precache trigger for {model_ct} ({manager_name}): Faulty Add Post-Acquire" in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    # The lock should be cleared by the finally block of the outer try-except
    assert cache.get(lock_key) is None