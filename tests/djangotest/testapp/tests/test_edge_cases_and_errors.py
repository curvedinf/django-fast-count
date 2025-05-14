import pytest
from django.core.cache import cache
from django.utils import timezone
import datetime # Added for datetime.timezone.utc
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock, ANY
from io import StringIO
import os
import time
from django.db import models as django_models # To avoid conflict with local 'models'
from testapp.models import TestModel # Main model for testing
from django_fast_count.models import FastCount
from django_fast_count.managers import FastCountModelManager, FastCountQuerySet, DISABLE_FORK_ENV_VAR
# Helper Models for specific test cases
class ModelWithBadFastCountQuerysets(django_models.Model):
    objects = FastCountModelManager()
    @classmethod
    def fast_count_querysets(cls):
        return "not a list or tuple" # Incorrect return type
    class Meta:
        app_label = "testapp"
class ModelWithDynamicallyAssignedManager(django_models.Model):
    some_field = django_models.BooleanField(default=True)
    class Meta:
        app_label = "testapp"
class AnotherTestModel(django_models.Model): # Model without FastCountManager
    name = django_models.CharField(max_length=100)
    objects = django_models.Manager()
    class Meta:
        app_label = "testapp"
class ModelWithSimpleManager(django_models.Model): # For manager discovery fallback
    data = django_models.CharField(max_length=10)
    objects = FastCountModelManager()
    @classmethod
    def fast_count_querysets(cls):
        return [cls.objects.filter(data="test")]
    class Meta:
        app_label = "testapp"
# Pytest marker for DB access for all tests in this module
pytestmark = pytest.mark.django_db
@pytest.fixture(autouse=True)
def clean_state_for_edge_cases():
    """Ensures a clean state for each test in this file."""
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    # Clean up instances of dynamically defined models if any were created
    # This might require more specific cleanup if tests actually create instances
    # For now, most tests mock interactions or use TestModel.
    ModelWithBadFastCountQuerysets.objects.all().delete()
    ModelWithDynamicallyAssignedManager.objects.all().delete()
    AnotherTestModel.objects.all().delete()
    ModelWithSimpleManager.objects.all().delete()
    # Reset env var if set by tests
    original_fork_setting = os.environ.pop(DISABLE_FORK_ENV_VAR, None)
    yield
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    ModelWithBadFastCountQuerysets.objects.all().delete()
    ModelWithDynamicallyAssignedManager.objects.all().delete()
    AnotherTestModel.objects.all().delete()
    ModelWithSimpleManager.objects.all().delete()
    if original_fork_setting is not None:
        os.environ[DISABLE_FORK_ENV_VAR] = original_fork_setting
    elif DISABLE_FORK_ENV_VAR in os.environ:
        del os.environ[DISABLE_FORK_ENV_VAR]
def create_test_models_deterministic(flag_true_count=0, flag_false_count=0):
    """Helper to create TestModel instances with specific flag counts."""
    TestModel.objects.bulk_create([TestModel(flag=True) for _ in range(flag_true_count)])
    TestModel.objects.bulk_create([TestModel(flag=False) for _ in range(flag_false_count)])
    return flag_true_count + flag_false_count
def test_fast_count_model_str_representation():
    create_test_models_deterministic(flag_true_count=1)
    model_instance = TestModel.objects.first()
    ct = ContentType.objects.get_for_model(model_instance)
    fc_entry = FastCount.objects.create(
        content_type=ct,
        manager_name="objects",
        queryset_hash="1234567890abcdef1234567890abcdef", # 32 chars
        count=100,
        expires_at=timezone.now() + timedelta(days=1)
    )
    expected_str = f"{ct} (objects) [12345678...]"
    assert str(fc_entry) == expected_str
def test_get_cache_key_fallback_on_sql_error(capsys):
    manager = TestModel.objects
    qs = manager.all()
    with patch.object(qs.query, "get_compiler", side_effect=Exception("SQL generation failed")):
        cache_key = manager._get_cache_key(qs)
    assert cache_key.startswith("fallback:")
    captured = capsys.readouterr()
    assert "Warning: Could not generate precise cache key for TestModel using SQL" in captured.out
    assert "SQL generation failed" in captured.out
def test_get_precache_querysets_handles_bad_return_type(capsys):
    manager = ModelWithBadFastCountQuerysets.objects
    ContentType.objects.get_for_model(ModelWithBadFastCountQuerysets) # Ensure CT type exists
    querysets = manager.get_precache_querysets()
    assert len(querysets) == 1
    expected_all_sql, _ = ModelWithBadFastCountQuerysets.objects.all().query.get_compiler(using=manager.db).as_sql()
    actual_precached_sql, _ = querysets[0].query.get_compiler(using=manager.db).as_sql()
    assert actual_precached_sql == expected_all_sql
    captured = capsys.readouterr()
    assert "ModelWithBadFastCountQuerysets.fast_count_querysets did not return a list or tuple." in captured.out
def test_count_handles_db_cache_get_error(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=5)
    manager = TestModel.objects
    qs = manager.all()
    cache_key = manager._get_cache_key(qs)
    cache.delete(cache_key)
    with patch("django_fast_count.managers.FastCount.objects.get") as mock_fc_get:
        mock_fc_get.side_effect = Exception("DB error on get")
        assert qs.count() == 5
    mock_fc_get.assert_called_once()
    captured = capsys.readouterr()
    assert "Error checking FastCount DB cache for TestModel" in captured.out
    assert "DB error on get" in captured.out
def test_count_handles_retroactive_db_cache_error(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=10)
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 5)
    manager = TestModel.objects
    qs = manager.all()
    cache_key = manager._get_cache_key(qs)
    cache.delete(cache_key)
    FastCount.objects.all().delete()
    with patch("django_fast_count.managers.FastCount.objects.update_or_create") as mock_update_create:
        mock_update_create.side_effect = Exception("DB error on update_or_create")
        assert qs.count() == 10
    mock_update_create.assert_called_once()
    captured = capsys.readouterr()
    assert "Error retroactively caching count in DB for TestModel" in captured.out
    assert "DB error on update_or_create" in captured.out
    assert cache.get(cache_key) == 10 # Django cache should still be populated
def test_precache_counts_handles_error_for_one_queryset(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=2, flag_false_count=3)
    manager = TestModel.objects
    original_qs_count = django_models.QuerySet.count
    def mock_qs_count_for_error(self_qs):
        sql_query_str = str(self_qs.query).upper() # Convert query to string and uppercase
        if "WHERE" in sql_query_str and "\"flag\" = True" in str(self_qs.query.where): # More specific check
            raise Exception("Simulated DB error for flag=True count")
        return original_qs_count(self_qs)
    
    with patch("django.db.models.query.QuerySet.count", side_effect=mock_qs_count_for_error):
        results = manager.precache_counts(manager_name="objects")
        
    captured = capsys.readouterr()
    key_all = manager._get_cache_key(manager.all())
    key_true = manager._get_cache_key(manager.filter(flag=True))
    key_false = manager._get_cache_key(manager.filter(flag=False))
    
    assert results[key_all] == 5
    assert isinstance(results[key_true], str) and "Error: Simulated DB error for flag=True count" in results[key_true]
    assert results[key_false] == 3
    assert "Error precaching count for TestModel queryset" in captured.out
    assert "Simulated DB error for flag=True count" in captured.out
    model_ct = ContentType.objects.get_for_model(TestModel)
    assert FastCount.objects.get(content_type=model_ct, manager_name="objects", queryset_hash=key_all).count == 5
    assert not FastCount.objects.filter(content_type=model_ct, manager_name="objects", queryset_hash=key_true).exists()
    assert FastCount.objects.get(content_type=model_ct, manager_name="objects", queryset_hash=key_false).count == 3
def test_maybe_trigger_precache_lock_not_acquired(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    with patch("django.core.cache.cache.add", return_value=False) as mock_cache_add:
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_cache_add.assert_called_once()
    captured = capsys.readouterr()
    assert "Precache lock fastcount:lock_precache:" in captured.out and "not acquired" in captured.out
def test_maybe_trigger_precache_synchronous_mode_success(monkeypatch, capsys):
    os.environ[DISABLE_FORK_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0, None)
    mock_precache_counts = MagicMock()
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts)
    current_time_ts = time.time()
    with patch("time.time", return_value=current_time_ts):
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_precache_counts.assert_called_once_with(manager_name=manager_name)
    captured = capsys.readouterr()
    assert f"SYNC_TEST_MODE: Forking disabled. Running precache_counts synchronously for {model_ct} ({manager_name})." in captured.out
    assert f"SYNC_TEST_MODE: precache_counts finished synchronously for {model_ct} ({manager_name})." in captured.out
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(last_run_key) == current_time_ts
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None
def test_maybe_trigger_precache_synchronous_mode_error(monkeypatch, capsys):
    os.environ[DISABLE_FORK_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    mock_precache_counts = MagicMock(side_effect=Exception("Sync precache error"))
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts)
    manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_precache_counts.assert_called_once_with(manager_name=manager_name)
    captured = capsys.readouterr()
    assert "SYNC_TEST_MODE: Forking disabled." in captured.out
    assert f"SYNC_TEST_MODE: Error in synchronous precache_counts for {model_ct} ({manager_name}): Sync precache error" in captured.out
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(last_run_key) == 0
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None
def test_get_manager_name_fallback_warning(monkeypatch, capsys):
    ContentType.objects.get_for_model(ModelWithDynamicallyAssignedManager)
    manager_instance = FastCountModelManager()
    qs = FastCountQuerySet(model=ModelWithDynamicallyAssignedManager)
    qs.manager = manager_instance
    manager_instance.model = ModelWithDynamicallyAssignedManager
    with patch.object(ModelWithDynamicallyAssignedManager, "__dict__", {}), \
         patch.object(ModelWithDynamicallyAssignedManager._meta, "managers_map", {}):
        manager_name = qs._get_manager_name()
    assert manager_name == "objects"
    captured = capsys.readouterr()
    assert "Warning: Could not determine manager name for ModelWithDynamicallyAssignedManager. Falling back to 'objects'." in captured.out
@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
def test_maybe_trigger_precache_forking_parent_path(mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys):
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    mock_os_fork.return_value = 12345
    current_ts = 1678886400.0
    mock_time.return_value = current_ts
    manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_os_fork.assert_called_once()
    mock_close_all.assert_not_called()
    mock_os_exit.assert_not_called()
    captured = capsys.readouterr()
    assert f"Forked background precache process 12345 for {model_ct} ({manager_name})." in captured.out
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) == "running"
@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
@patch.object(FastCountModelManager, "precache_counts")
def test_maybe_trigger_precache_forking_child_path_success(mock_precache_counts_method, mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys):
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    mock_os_fork.return_value = 0
    child_pid = 54321
    with patch("os.getpid", return_value=child_pid):
        current_ts = 1678886400.0
        mock_time.return_value = current_ts
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_os_fork.assert_called_once()
    mock_close_all.assert_called_once()
    mock_precache_counts_method.assert_called_once_with(manager_name=manager_name)
    mock_os_exit.assert_called_once_with(0)
    captured = capsys.readouterr()
    assert f"Background precache process (PID {child_pid}) starting for {model_ct} ({manager_name})." in captured.out
    assert f"Background precache process (PID {child_pid}) finished successfully." in captured.out
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(last_run_key) == current_ts
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None
@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
@patch.object(FastCountModelManager, "precache_counts")
def test_maybe_trigger_precache_forking_child_path_error(mock_precache_counts_method, mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys):
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    original_last_run_time = 0
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), original_last_run_time)
    mock_os_fork.return_value = 0
    mock_precache_counts_method.side_effect = Exception("Child precache error")
    child_pid = 54321
    with patch("os.getpid", return_value=child_pid):
        current_ts = 1678886400.0
        mock_time.return_value = current_ts
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_os_fork.assert_called_once()
    mock_close_all.assert_called_once()
    mock_precache_counts_method.assert_called_once_with(manager_name=manager_name)
    mock_os_exit.assert_called_once_with(1)
    captured = capsys.readouterr()
    assert f"Background precache process (PID {child_pid}) starting for {model_ct} ({manager_name})." in captured.out
    assert f"Background precache process (PID {child_pid}) failed: Child precache error" in captured.out
    last_run_key = manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(last_run_key) == original_last_run_time
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    assert cache.get(lock_key) is None
@patch("os.fork")
@patch("django.core.cache.cache.delete")
def test_maybe_trigger_precache_forking_os_error(mock_cache_delete, mock_os_fork, monkeypatch, capsys):
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    mock_os_fork.side_effect = OSError("Fork failed miserably")
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    with patch("django.core.cache.cache.add", return_value=True) as mock_cache_add_specific:
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_os_fork.assert_called_once() # This will be called once in the flow
    captured = capsys.readouterr()
    assert f"Error forking/managing precache process for {model_ct} ({manager_name}): Fork failed miserably" in captured.out
    mock_cache_add_specific.assert_called_with(lock_key, "running", manager.precache_lock_timeout)
    mock_cache_delete.assert_called_with(lock_key)
@patch("os.fork", side_effect=Exception("Some other fork setup error"))
@patch("django.core.cache.cache.delete")
def test_maybe_trigger_precache_forking_generic_exception(mock_cache_delete, mock_os_fork_generic_error, monkeypatch, capsys):
    if DISABLE_FORK_ENV_VAR in os.environ: del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(manager._precache_last_run_key_template.format(ct_id=model_ct.id, manager=manager_name), 0)
    lock_key = manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)
    with patch("django.core.cache.cache.add", return_value=True) as mock_cache_add_specific:
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    captured = capsys.readouterr()
    assert f"Unexpected error during precache trigger for {model_ct} ({manager_name}): Some other fork setup error" in captured.out
    mock_cache_add_specific.assert_called_with(lock_key, "running", manager.precache_lock_timeout)
    mock_cache_delete.assert_called_with(lock_key)
def test_precache_command_no_fastcount_managers(capsys):
    ContentType.objects.get_for_model(AnotherTestModel) # Ensure CT exists
    AnotherTestModel.objects.create(name="test")
    with patch("django.apps.apps.get_models", return_value=[AnotherTestModel]):
        call_command("precache_fast_counts")
    captured = capsys.readouterr()
    assert "No models found using FastCountModelManager. No counts were precached." in captured.out
def test_precache_command_handles_error_in_manager_precache(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    original_manager_precache_counts = TestModel.objects.precache_counts
    def faulty_precache_counts(manager_name):
        results = original_manager_precache_counts(manager_name=manager_name)
        if results:
            first_key = list(results.keys())[0]
            results[first_key] = "Simulated Error during precache"
        return results
    monkeypatch.setattr(TestModel.objects, "precache_counts", faulty_precache_counts)
    stdout_capture = StringIO() # For specific stdout checking if needed
    call_command("precache_fast_counts", stdout=stdout_capture)
    captured_out = stdout_capture.getvalue() # What BaseCommand wrote to stdout
    # capsys.readouterr() would also work but this is more direct for BaseCommand's stdout
    assert "Processing: testapp.TestModel (manager: 'objects')" in captured_out
    assert "Precached counts for 3 querysets:" in captured_out
    assert "Simulated Error during precache" in captured_out # Warnings go to stdout
def test_precache_command_manager_discovery_fallback(monkeypatch, capsys):
    ContentType.objects.get_for_model(ModelWithSimpleManager)
    ModelWithSimpleManager.objects.create(data="test")
    ModelWithSimpleManager.objects.create(data="another")
    original_getattr = builtins.getattr
    def mock_getattr_for_managers_map(obj, name, *default_args):
        if isinstance(obj, django_models.options.Options) and obj.model == ModelWithSimpleManager and name == "managers_map":
            return {} # Simulate empty managers_map
        if default_args:
            return original_getattr(obj, name, default_args[0])
        return original_getattr(obj, name)
    
    # Patch builtins.getattr because it's used directly in the command
    import builtins
    with patch("builtins.getattr", side_effect=mock_getattr_for_managers_map):
        with patch("django.apps.apps.get_models", return_value=[ModelWithSimpleManager]):
            stdout_capture = StringIO()
            call_command("precache_fast_counts", stdout=stdout_capture)
            
    captured_out = stdout_capture.getvalue()
    assert "Processing: testapp.ModelWithSimpleManager (manager: 'objects')" in captured_out
    assert "Precached counts for 2 querysets:" in captured_out # .all() and .filter(data="test")
    # .all() count is 2, .filter(data="test") is 1
    # Check that counts are reported correctly (e.g. "Hash xxxxxxxx...: 2" and "Hash yyyyyyyy...: 1")
    assert ": 2" in captured_out # Count for .all()
    assert ": 1" in captured_out # Count for .filter(data="test")