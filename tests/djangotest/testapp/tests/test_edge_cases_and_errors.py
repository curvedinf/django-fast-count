import pytest
from django.core.cache import cache
from django.utils import timezone
import datetime  # Added for datetime.timezone.utc
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, MagicMock, ANY, PropertyMock
from io import StringIO
import os
import time
import builtins  # Import builtins at the module level or ensure it's imported in test_precache_command_manager_discovery_fallback before use
from django.db import models as django_models  # To avoid conflict with local 'models'
from django_fast_count.models import FastCount
from django_fast_count.managers import (
    FastCountModelManager,
    FastCountQuerySet,
    DISABLE_FORK_ENV_VAR,
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
    manager = TestModel.objects
    qs = manager.all()
    with patch.object(
        qs.query, "get_compiler", side_effect=Exception("SQL generation failed")
    ):
        cache_key = manager._get_cache_key(qs)
    assert cache_key.startswith("fallback:")
    captured = capsys.readouterr()
    assert (
        "Warning: Could not generate precise cache key for TestModel using SQL"
        in captured.out
    )
    assert "SQL generation failed" in captured.out


def test_get_precache_querysets_handles_bad_return_type(capsys):
    manager = ModelWithBadFastCountQuerysets.objects
    ContentType.objects.get_for_model(
        ModelWithBadFastCountQuerysets
    )  # Ensure CT type exists
    querysets = manager.get_precache_querysets()
    assert len(querysets) == 1
    expected_all_sql, _ = (
        ModelWithBadFastCountQuerysets.objects.all()
        .query.get_compiler(using=manager.db)
        .as_sql()
    )
    actual_precached_sql, _ = querysets[0].query.get_compiler(using=manager.db).as_sql()
    assert actual_precached_sql == expected_all_sql
    captured = capsys.readouterr()
    assert (
        "ModelWithBadFastCountQuerysets.fast_count_querysets did not return a list or tuple."
        in captured.out
    )


def test_precache_counts_handles_error_for_one_queryset(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=2, flag_false_count=3)
    manager = TestModel.objects
    original_qs_count = django_models.QuerySet.count  # Unbound method

    def mock_qs_count_for_error(self_qs):
        if not isinstance(self_qs, django_models.QuerySet):
            raise TypeError(f"Expected QuerySet, got {type(self_qs)}")

        # Robust check for flag=True filter
        # This checks the structure of the WHERE clause for flag=True
        is_flag_true_filter = False
        if hasattr(self_qs.query, "where") and self_qs.query.where:
            for child_node in self_qs.query.where.children:
                if hasattr(child_node, "lhs") and hasattr(child_node, "rhs"):
                    lookup_field_name = None
                    # Try to get field name from LHS expression
                    if hasattr(child_node.lhs, "target") and hasattr(
                        child_node.lhs.target, "name"
                    ):  # Direct Col
                        lookup_field_name = child_node.lhs.target.name
                    elif (
                        hasattr(child_node.lhs, "lhs")
                        and hasattr(child_node.lhs.lhs, "target")
                        and hasattr(child_node.lhs.lhs.target, "name")
                    ):  # Nested Col like in F() expressions
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
        results = manager.precache_counts(manager_name="objects")

    captured = capsys.readouterr()
    key_all = manager._get_cache_key(manager.all())
    key_true = manager._get_cache_key(manager.filter(flag=True))
    key_false = manager._get_cache_key(manager.filter(flag=False))

    assert results[key_all] == 5
    assert (
        isinstance(results[key_true], str)
        and "Error: Simulated DB error for flag=True count" in results[key_true]
    )
    assert results[key_false] == 3

    assert "Error precaching count for TestModel queryset" in captured.out
    assert "Simulated DB error for flag=True count" in captured.out

    model_ct = ContentType.objects.get_for_model(TestModel)
    assert (
        FastCount.objects.get(
            content_type=model_ct, manager_name="objects", queryset_hash=key_all
        ).count
        == 5
    )
    assert not FastCount.objects.filter(
        content_type=model_ct, manager_name="objects", queryset_hash=key_true
    ).exists()
    assert (
        FastCount.objects.get(
            content_type=model_ct, manager_name="objects", queryset_hash=key_false
        ).count
        == 3
    )


def test_maybe_trigger_precache_lock_not_acquired(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    # Ensure last_run_key is old enough to trigger
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        time.time() - manager.precache_count_every.total_seconds() * 2,
        timeout=None,
    )
    with patch(
        "django.core.cache.cache.add", return_value=False
    ) as mock_cache_add:  # Simulate lock not acquired
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_cache_add.assert_called_once()
    captured = capsys.readouterr()
    assert (
        f"Precache lock {manager._precache_lock_key_template.format(ct_id=model_ct.id, manager=manager_name)} not acquired"
        in captured.out
    )


def test_maybe_trigger_precache_synchronous_mode_success(monkeypatch, capsys):
    os.environ[DISABLE_FORK_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    # Ensure last_run_key is old enough to trigger; use a fixed past timestamp for predictability
    initial_last_run_ts = time.time() - manager.precache_count_every.total_seconds() * 2
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        initial_last_run_ts,
        timeout=None,
    )

    mock_precache_counts_instance = MagicMock()
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts_instance)

    current_time_ts = time.time()  # Use a fresh timestamp for "now"
    with patch(
        "time.time", return_value=current_time_ts
    ):  # Mock time.time() to control "now"
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    mock_precache_counts_instance.assert_called_once_with(manager_name=manager_name)
    captured = capsys.readouterr()
    assert (
        f"SYNC_TEST_MODE: Forking disabled. Running precache_counts synchronously for {model_ct} ({manager_name})."
        in captured.out
    )
    assert (
        f"SYNC_TEST_MODE: precache_counts finished synchronously for {model_ct} ({manager_name})."
        in captured.out
    )
    last_run_key = manager._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert (
        cache.get(last_run_key) == current_time_ts
    )  # Should be updated to the "now" timestamp
    lock_key = manager._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None


def test_maybe_trigger_precache_synchronous_mode_error(monkeypatch, capsys):
    os.environ[DISABLE_FORK_ENV_VAR] = "1"
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    initial_last_run_ts = time.time() - manager.precache_count_every.total_seconds() * 2
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        initial_last_run_ts,
        timeout=None,
    )

    mock_precache_counts_instance = MagicMock(
        side_effect=Exception("Sync precache error")
    )
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts_instance)

    current_time_ts = time.time()  # Use a fresh timestamp for "now"
    with patch("time.time", return_value=current_time_ts):
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    mock_precache_counts_instance.assert_called_once_with(manager_name=manager_name)
    captured = capsys.readouterr()
    assert "SYNC_TEST_MODE: Forking disabled." in captured.out
    assert (
        f"SYNC_TEST_MODE: Error in synchronous precache_counts for {model_ct} ({manager_name}): Sync precache error"
        in captured.out
    )
    last_run_key = manager._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert (
        cache.get(last_run_key) == initial_last_run_ts
    )  # Should NOT be updated on error
    lock_key = manager._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None


def test_get_manager_name_fallback_warning(monkeypatch, capsys):
    ContentType.objects.get_for_model(ModelWithDynamicallyAssignedManager)
    manager_instance = FastCountModelManager()
    qs = FastCountQuerySet(model=ModelWithDynamicallyAssignedManager)
    qs.manager = manager_instance
    manager_instance.model = ModelWithDynamicallyAssignedManager
    # ModelWithDynamicallyAssignedManager has no explicit manager named 'objects' or similar
    # Patching managers_map to be empty ensures the second lookup path also fails.
    with patch.object(ModelWithDynamicallyAssignedManager._meta, "managers_map", {}):
        manager_name = qs._get_manager_name()
    assert manager_name == "objects"  # Default fallback
    captured = capsys.readouterr()
    assert (
        "Warning: Could not determine manager name for ModelWithDynamicallyAssignedManager. Falling back to 'objects'."
        in captured.out
    )


@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
def test_maybe_trigger_precache_forking_parent_path(
    mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys
):
    initial_fixed_ts = 1678880000.0
    mock_time.return_value = initial_fixed_ts
    if DISABLE_FORK_ENV_VAR in os.environ:
        del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        0,
        timeout=None,
    )
    mock_os_fork.return_value = 12345  # Simulate parent process path
    current_ts_for_logic = 1678886400.0
    mock_time.return_value = current_ts_for_logic
    manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)
    mock_os_fork.assert_called_once()
    mock_close_all.assert_not_called()
    mock_os_exit.assert_not_called()
    captured = capsys.readouterr()
    assert (
        f"Forked background precache process 12345 for {model_ct} ({manager_name})."
        in captured.out
    )
    lock_key = manager._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) == "running"


@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
# Removed @patch.object(FastCountModelManager, "precache_counts")
def test_maybe_trigger_precache_forking_child_path_success(
    mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys
):
    initial_fixed_ts = 1678880000.0
    mock_time.return_value = initial_fixed_ts
    if DISABLE_FORK_ENV_VAR in os.environ:
        del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        0,
        timeout=None,
    )

    # Mock the instance method directly
    mock_precache_counts_on_instance = MagicMock()
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts_on_instance)

    mock_os_fork.return_value = 0  # Simulate child process path
    child_pid = 54321
    current_ts_for_logic = 1678886400.0
    mock_time.return_value = current_ts_for_logic
    with patch("os.getpid", return_value=child_pid):
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    mock_os_fork.assert_called_once()
    mock_close_all.assert_called_once()
    mock_precache_counts_on_instance.assert_called_once_with(manager_name=manager_name)
    mock_os_exit.assert_called_once_with(0)
    captured = capsys.readouterr()
    assert (
        f"Background precache process (PID {child_pid}) starting for {model_ct} ({manager_name})."
        in captured.out
    )
    assert (
        f"Background precache process (PID {child_pid}) finished successfully."
        in captured.out
    )
    last_run_key = manager._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(last_run_key) == current_ts_for_logic
    lock_key = manager._precache_lock_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(lock_key) is None


@patch("os.fork")
@patch("os._exit")
@patch("django.db.connections.close_all")
@patch("time.time")
# Removed @patch.object(FastCountModelManager, "precache_counts")
def test_maybe_trigger_precache_forking_child_path_error(
    mock_time, mock_close_all, mock_os_exit, mock_os_fork, monkeypatch, capsys
):
    initial_fixed_ts = 1678880000.0
    mock_time.return_value = initial_fixed_ts
    if DISABLE_FORK_ENV_VAR in os.environ:
        del os.environ[DISABLE_FORK_ENV_VAR]
    create_test_models_deterministic(flag_true_count=1)
    manager = TestModel.objects
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager_name = "objects"
    monkeypatch.setattr(manager, "precache_count_every", timedelta(seconds=1))
    original_last_run_time_value = 0
    cache.set(
        manager._precache_last_run_key_template.format(
            ct_id=model_ct.id, manager=manager_name
        ),
        original_last_run_time_value,
        timeout=None,
    )

    # Mock the instance method directly
    mock_precache_counts_on_instance = MagicMock(
        side_effect=Exception("Child precache error")
    )
    monkeypatch.setattr(manager, "precache_counts", mock_precache_counts_on_instance)

    mock_os_fork.return_value = 0  # Simulate child process path
    child_pid = 54321
    current_ts_for_logic = 1678886400.0
    mock_time.return_value = current_ts_for_logic
    with patch("os.getpid", return_value=child_pid):
        manager.maybe_trigger_precache(manager_name=manager_name, model_ct=model_ct)

    mock_os_fork.assert_called_once()
    mock_close_all.assert_called_once()
    mock_precache_counts_on_instance.assert_called_once_with(manager_name=manager_name)
    mock_os_exit.assert_called_once_with(1)
    captured = capsys.readouterr()
    assert (
        f"Background precache process (PID {child_pid}) starting for {model_ct} ({manager_name})."
        in captured.out
    )
    assert (
        f"Background precache process (PID {child_pid}) failed: Child precache error"
        in captured.out
    )
    last_run_key = manager._precache_last_run_key_template.format(
        ct_id=model_ct.id, manager=manager_name
    )
    assert cache.get(last_run_key) == original_last_run_time_value
    lock_key = manager._precache_lock_key_template.format(
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
        "No models found using FastCountModelManager. No counts were precached."
        in captured.out
    )


def test_precache_command_handles_error_in_manager_precache(monkeypatch, capsys):
    create_test_models_deterministic(flag_true_count=1)
    original_manager_precache_counts_method = TestModel.objects.precache_counts

    def faulty_precache_counts(manager_name):
        results = original_manager_precache_counts_method(manager_name=manager_name)
        if results:
            first_key = list(results.keys())[0]
            results[first_key] = "Simulated Error during precache"
        return results

    monkeypatch.setattr(TestModel.objects, "precache_counts", faulty_precache_counts)
    stdout_capture = StringIO()
    call_command("precache_fast_counts", stdout=stdout_capture)
    captured_out = stdout_capture.getvalue()
    assert "Processing: testapp.TestModel (manager: 'objects')" in captured_out
    assert (
        f"Precached counts for {len(TestModel.objects.get_precache_querysets())} querysets:"
        in captured_out
    )
    assert "Simulated Error during precache" in captured_out
