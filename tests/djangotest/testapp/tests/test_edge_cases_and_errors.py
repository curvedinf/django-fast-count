import pytest
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock, call  # Added call
from io import StringIO
import os
import time
import subprocess  # Added for subprocess.DEVNULL
import sys  # Added for sys.executable
from django.db import models as django_models  # To avoid conflict with local 'models'
from django_fast_count.models import FastCount
from django_fast_count.managers import (
    FastCountManager,
    FastCountQuerySet,
    FORCE_SYNC_PRECACHE_ENV_VAR,
)
from testapp.models import (
    ModelWithBadFastCountQuerysets,
    ModelWithDynamicallyAssignedManager,
    AnotherTestModel,
    ModelWithSimpleManager,
    TestModel,
)
# Pytest marker for DB access for all tests in this module
pytestmark = pytest.mark.django_db
@pytest.fixture(autouse=True)
def clean_state_for_edge_cases(settings):
    """Ensures a clean state for each test in this file."""
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    ModelWithBadFastCountQuerysets.objects.all().delete()
    ModelWithDynamicallyAssignedManager.objects.all().delete()
    AnotherTestModel.objects.all().delete()
    ModelWithSimpleManager.objects.all().delete()
    # Store original BASE_DIR and environment variables
    original_base_dir = getattr(settings, "BASE_DIR", None)
    original_sync_setting = os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None)
    # Set BASE_DIR for tests that might need it (e.g., subprocess calls to manage.py)
    # Use a sensible default like the current working directory if not already set,
    # or a specific path if your test setup requires it.
    # For tests, django_test_project_dir is typically the CWD for pytest.
    settings.BASE_DIR = os.getcwd()
    yield
    cache.clear()
    FastCount.objects.all().delete()
    TestModel.objects.all().delete()
    ModelWithBadFastCountQuerysets.objects.all().delete()
    ModelWithDynamicallyAssignedManager.objects.all().delete()
    AnotherTestModel.objects.all().delete()
    ModelWithSimpleManager.objects.all().delete()
    # Restore original BASE_DIR and environment variables
    if original_base_dir is not None:
        settings.BASE_DIR = original_base_dir
    elif hasattr(
        settings, "BASE_DIR"
    ):  # if it was set by this fixture and not originally present
        delattr(settings, "BASE_DIR")
    if original_sync_setting is not None:
        os.environ[FORCE_SYNC_PRECACHE_ENV_VAR] = original_sync_setting
    elif FORCE_SYNC_PRECACHE_ENV_VAR in os.environ:  # if set by test but not originally
        del os.environ[FORCE_SYNC_PRECACHE_ENV_VAR]
def create_test_models_deterministic(flag_true_count=0, flag_false_count=0):
    """Helper to create TestModel instances with specific flag counts."""
    TestModel.objects.bulk_create(
        [TestModel(flag=True) for _ in range(flag_true_count)]
    )
    TestModel.objects.bulk_create(
        [TestModel(flag=False) for _ in range(flag_false_count)]
    )
    return flag_true_count + flag_false_count
def test_fast_count_model_str_representation():
    create_test_models_deterministic(flag_true_count=1)
    model_instance = TestModel.objects.first()
    ct = ContentType.objects.get_for_model(model_instance)
    fc_entry = FastCount.objects.create(
        content_type=ct,
        manager_name="objects",
        queryset_hash="1234567890abcdef1234567890abcdef",  # 32 chars
        count=100,
        expires_at=timezone.now() + timedelta(days=1),
    )
    expected_str = f"{ct} (objects) [12345678...]"
    assert str(fc_entry) == expected_str
def test_get_cache_key_fallback_on_sql_error(capsys):
    qs = TestModel.objects.all()
    with patch.object(
        qs.query, "get_compiler", side_effect=Exception("SQL generation failed")
    ):
        cache_key = qs._get_cache_key()
    assert cache_key.startswith("fallback:")
    captured = capsys.readouterr()
    assert (
        f"Warning: Could not generate precise cache key for {TestModel.__name__} using SQL"
        in captured.out
    )
    assert "SQL generation failed" in captured.out
def test_get_precache_querysets_handles_bad_return_type(capsys):
    qs = ModelWithBadFastCountQuerysets.objects.all()
    ContentType.objects.get_for_model(
        ModelWithBadFastCountQuerysets
    )  # Ensure CT type exists
    querysets = qs.get_precache_querysets()
    assert len(querysets) == 1
    expected_all_sql, _ = (
        ModelWithBadFastCountQuerysets.objects.all()
        .query.get_compiler(using=qs.db)
        .as_sql()
    )
    actual_precached_sql, _ = querysets[0].query.get_compiler(using=qs.db).as_sql()
    assert actual_precached_sql == expected_all_sql
    captured = capsys.readouterr()
    assert (
        f"{ModelWithBadFastCountQuerysets.__name__}.fast_count_querysets did not return a list or tuple."
        in captured.out
    )
def test_precache_counts_handles_error_for_one_queryset(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=2, flag_false_count=3)
    qs_for_precache = TestModel.objects.all()  # Get a QS instance
    original_qs_count = django_models.QuerySet.count  # Unbound method
    def mock_qs_count_for_error(self_qs):
        if not isinstance(self_qs, django_models.QuerySet):
            raise TypeError(f"Expected QuerySet, got {type(self_qs)}")
        # Robust check for flag=True filter
        is_flag_true_filter = False
        if hasattr(self_qs.query, "where") and self_qs.query.where:
            for child_node in self_qs.query.where.children:
                if hasattr(child_node, "lhs") and hasattr(child_node, "rhs"):
                    lookup_field_name = None
                    if hasattr(child_node.lhs, "target") and hasattr(
                        child_node.lhs.target, "name"
                    ):
                        lookup_field_name = child_node.lhs.target.name
                    elif (
                        hasattr(child_node.lhs, "lhs")
                        and hasattr(child_node.lhs.lhs, "target")
                        and hasattr(child_node.lhs.lhs.target, "name")
                    ):
                        lookup_field_name = child_node.lhs.lhs.target.name
                    if lookup_field_name == "flag" and child_node.rhs is True:
                        is_flag_true_filter = True
                        break
        if is_flag_true_filter:
            raise Exception("Simulated DB error for flag=True count")
        return original_qs_count(self_qs)
    with patch(
        "django.db.models.query.QuerySet.count",
        autospec=True,
        side_effect=mock_qs_count_for_error,
    ):
        results = qs_for_precache.precache_counts()
    captured = capsys.readouterr()
    qs_all = TestModel.objects.all()
    qs_true = TestModel.objects.filter(flag=True)
    qs_false = TestModel.objects.filter(flag=False)
    key_all = qs_all._get_cache_key()
    key_true = qs_true._get_cache_key()
    key_false = qs_false._get_cache_key()
    assert results[key_all] == 5
    assert (
        isinstance(results[key_true], str)
        and "Error: Simulated DB error for flag=True count" in results[key_true]
    )
    assert results[key_false] == 3
    assert (
        f"Error precaching count for {TestModel.__name__} (manager: objects) queryset"
        in captured.out
    )  # manager name is from qs instance
    assert "Simulated DB error for flag=True count" in captured.out
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs_for_precache.manager_name  # Get manager name from the QS instance
    assert (
        FastCount.objects.get(
            content_type=model_ct, manager_name=manager_name, queryset_hash=key_all
        ).count
        == 5
    )
    assert not FastCount.objects.filter(
        content_type=model_ct, manager_name=manager_name, queryset_hash=key_true
    ).exists()
    assert (
        FastCount.objects.get(
            content_type=model_ct, manager_name=manager_name, queryset_hash=key_false
        ).count
        == 3
    )
def test_maybe_trigger_precache_lock_not_acquired(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs.manager_name
    model_name = qs.model.__name__
    monkeypatch.setattr(qs, "precache_count_every", timedelta(seconds=1))
    # Ensure last_run_key is old enough to trigger
    cache.set(
        qs._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        time.time() - qs.precache_count_every.total_seconds() * 2,
        timeout=None,
    )
    with patch(
        "django.core.cache.cache.add", return_value=False
    ) as mock_cache_add:  # Simulate lock not acquired
        qs.maybe_trigger_precache()
    mock_cache_add.assert_called_once()
    captured = capsys.readouterr()
    assert (
        f"Precache lock {qs._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)} not acquired for {model_name} ({manager_name}). Process already running or recently finished/failed."
        in captured.out
    )
def test_maybe_trigger_precache_synchronous_mode_success(monkeypatch, capsys, settings):
    os.environ[FORCE_SYNC_PRECACHE_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs.manager_name
    model_name = qs.model.__name__
    monkeypatch.setattr(qs, "precache_count_every", timedelta(seconds=1))
    initial_last_run_ts = time.time() - qs.precache_count_every.total_seconds() * 2
    cache.set(
        qs._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        initial_last_run_ts,
        timeout=None,
    )
    mock_precache_counts_instance = MagicMock()
    monkeypatch.setattr(qs, "precache_counts", mock_precache_counts_instance)
    current_time_ts = time.time()
    with patch("time.time", return_value=current_time_ts):
        qs.maybe_trigger_precache()  # This will access settings.BASE_DIR if not sync (but it is)
    mock_precache_counts_instance.assert_called_once_with()
    captured = capsys.readouterr()
    assert (
        f"SYNC_MODE: Running precache_counts synchronously for {model_name} ({manager_name})."
        in captured.out
    )
    assert (
        f"SYNC_MODE: precache_counts finished synchronously for {model_name} ({manager_name})."
        in captured.out
    )
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(last_run_key) == current_time_ts
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None
def test_maybe_trigger_precache_synchronous_mode_error(monkeypatch, capsys, settings):
    os.environ[FORCE_SYNC_PRECACHE_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs.manager_name
    model_name = qs.model.__name__
    monkeypatch.setattr(qs, "precache_count_every", timedelta(seconds=1))
    initial_last_run_ts = time.time() - qs.precache_count_every.total_seconds() * 2
    cache.set(
        qs._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        initial_last_run_ts,
        timeout=None,
    )
    mock_precache_counts_instance = MagicMock(
        side_effect=Exception("Sync precache error")
    )
    monkeypatch.setattr(qs, "precache_counts", mock_precache_counts_instance)
    current_time_ts = time.time()
    with patch("time.time", return_value=current_time_ts):
        qs.maybe_trigger_precache()  # Accesses settings.BASE_DIR if not sync (but it is)
    mock_precache_counts_instance.assert_called_once_with()
    captured = capsys.readouterr()
    assert "SYNC_MODE: Running precache_counts synchronously" in captured.out
    assert (
        f"SYNC_MODE: Error in synchronous precache_counts for {model_name} ({manager_name}): Sync precache error"
        in captured.out
    )
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert (
        cache.get(last_run_key) == initial_last_run_ts
    )  # Should not be updated on error
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None
@patch("subprocess.Popen")
@patch("time.time")
def test_maybe_trigger_precache_subprocess_launch_success(
    mock_time, mock_subprocess_popen, monkeypatch, capsys, settings
):
    # settings.BASE_DIR is set by the fixture
    initial_fixed_ts = 1678880000.0
    mock_time.return_value = initial_fixed_ts
    os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None)  # Ensure not in sync mode
    create_test_models_deterministic(flag_true_count=1)
    qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs.manager_name
    model_name = qs.model.__name__
    monkeypatch.setattr(qs, "precache_count_every", timedelta(seconds=1))
    cache.set(
        qs._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        0,  # Ensure it's due to run
        timeout=None,
    )
    mock_process = MagicMock()
    mock_process.pid = 12345
    mock_subprocess_popen.return_value = mock_process
    current_ts_for_logic = 1678886400.0
    mock_time.return_value = current_ts_for_logic
    qs.maybe_trigger_precache()
    expected_manage_py_path = os.path.join(settings.BASE_DIR, "manage.py")
    expected_cmd = [sys.executable, expected_manage_py_path, "precache_fast_counts"]
    mock_subprocess_popen.assert_called_once_with(
        expected_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    captured = capsys.readouterr()
    assert (
        f"Launched background precache process (PID 12345) for {model_name} ({manager_name})."
        in captured.out
    )
    assert (  # Also check the "Attempting to launch" message
        f"Attempting to launch background precache command for {model_name} ({manager_name})."
        in captured.out
    )
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(last_run_key) == current_ts_for_logic
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None
@patch("time.time")
@patch("subprocess.Popen")
def test_maybe_trigger_precache_subprocess_launch_error(
    mock_subprocess_popen, mock_time, monkeypatch, capsys, settings
):
    # settings.BASE_DIR is set by the fixture
    initial_fixed_ts = 1678880000.0
    mock_time.return_value = initial_fixed_ts
    os.environ.pop(FORCE_SYNC_PRECACHE_ENV_VAR, None)
    create_test_models_deterministic(flag_true_count=1)
    qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = qs.manager_name
    model_name = qs.model.__name__
    monkeypatch.setattr(qs, "precache_count_every", timedelta(seconds=1))
    original_last_run_time_value = 0
    last_run_key = qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    cache.set(last_run_key, original_last_run_time_value, timeout=None)
    mock_subprocess_popen.side_effect = Exception("Subprocess launch failed")
    current_ts_for_logic = 1678886400.0  # This time won't be set for last_run_key
    mock_time.return_value = current_ts_for_logic
    qs.maybe_trigger_precache()
    mock_subprocess_popen.assert_called_once()
    captured = capsys.readouterr()
    assert (
        f"Error launching background precache command for {model_name} ({manager_name}): Subprocess launch failed"
        in captured.out
    )
    assert cache.get(last_run_key) == original_last_run_time_value
    lock_key = qs._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None
def test_precache_command_no_fastcount_managers(capsys):
    ContentType.objects.get_for_model(AnotherTestModel)
    AnotherTestModel.objects.create(name="test")
    with patch("django.apps.apps.get_models", return_value=[AnotherTestModel]):
        call_command("precache_fast_counts")
    captured = capsys.readouterr()
    assert (
        "No models found using FastCountManager. No counts were precached."
        in captured.out
    )
def test_precache_command_handles_error_in_manager_precache(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    original_qs_precache_counts_method = FastCountQuerySet.precache_counts
    def faulty_precache_counts(self_qs):  # self_qs is the FastCountQuerySet instance
        results = original_qs_precache_counts_method(self_qs)
        if results:  # Make sure results is not empty
            first_key = list(results.keys())[0]
            results[first_key] = "Simulated Error during precache"
        return results
    monkeypatch.setattr(FastCountQuerySet, "precache_counts", faulty_precache_counts)
    stdout_capture = StringIO()
    call_command("precache_fast_counts", stdout=stdout_capture)
    captured_out = stdout_capture.getvalue()
    assert (
        f"Processing: testapp.{TestModel.__name__} (manager: 'objects')" in captured_out
    )
    # The number of querysets is obtained from the QS instance
    num_querysets = len(TestModel.objects.all().get_precache_querysets())
    assert f"Precached counts for {num_querysets} querysets:" in captured_out
    assert "Simulated Error during precache" in captured_out
def test_maybe_trigger_precache_disabled_by_manager_flag(monkeypatch):
    """
    Tests that maybe_trigger_precache is a no-op when disable_forked_precaching=True.
    """
    # Replace the manager on TestModel with one that has the flag enabled.
    # We also set a very short precache_count_every to ensure the trigger
    # logic would normally fire.
    new_manager = FastCountManager(
        disable_forked_precaching=True,
        precache_count_every=timedelta(seconds=1),
    )
    monkeypatch.setattr(TestModel, "objects", new_manager)
    create_test_models_deterministic(flag_true_count=1)
    # Manually set the last run time to be in the past to ensure the
    # precache condition is met.
    configured_qs = TestModel.objects.all()
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = configured_qs.manager_name
    last_run_key = configured_qs._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    cache.set(last_run_key, time.time() - 2, timeout=None)
    # The key check is to see if `cache.add` is called for locking.
    # If disable_forked_precaching works, maybe_trigger_precache returns early,
    # and cache.add is never called.
    with patch("django.core.cache.cache.add") as mock_cache_add:
        # Calling .count() will invoke maybe_trigger_precache.
        TestModel.objects.count()
        mock_cache_add.assert_not_called()
