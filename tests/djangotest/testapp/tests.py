import pytest
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, ANY

from .models import TestModel
from django_fast_count.models import FastCount
from django_fast_count.managers import FastCountModelManager

# Pytest marker for DB access for all tests in this module
pytestmark = pytest.mark.django_db

@pytest.fixture(autouse=True)
def clear_caches_and_fastcount_db_for_each_test():
    """Ensures a clean state for each test."""
    cache.clear()
    FastCount.objects.all().delete()
    yield
    cache.clear()
    FastCount.objects.all().delete()

def create_test_models_deterministic(flag_true_count=0, flag_false_count=0):
    """Helper to create TestModel instances with specific flag counts."""
    TestModel.objects.bulk_create([TestModel(flag=True) for _ in range(flag_true_count)])
    TestModel.objects.bulk_create([TestModel(flag=False) for _ in range(flag_false_count)])
    return flag_true_count + flag_false_count

def test_initial_count_no_cache():
    """Test basic counting without any caching triggered."""
    create_test_models_deterministic(flag_true_count=2, flag_false_count=3) # Total 5
    
    # Default cache_counts_larger_than is 1_000 for TestModel.objects
    assert TestModel.objects.count() == 5
    assert TestModel.objects.filter(flag=True).count() == 2
    assert TestModel.objects.filter(flag=False).count() == 3

    assert FastCount.objects.count() == 0 # No DB cache entries

    # Ensure Django cache's set method was not called for these counts
    with patch("django.core.cache.cache.set") as mock_cache_set:
        TestModel.objects.count()
        TestModel.objects.filter(flag=True).count()
        mock_cache_set.assert_not_called()

def test_retroactive_caching(monkeypatch):
    """Test retroactive caching when count exceeds threshold."""
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 2)
    # expire_cached_counts_after is 1 minute for TestModel.objects

    create_test_models_deterministic(flag_true_count=1, flag_false_count=3) # Total 4

    # Count for .all() is 4, threshold is 2. Should cache.
    assert TestModel.objects.count() == 4
    
    model_ct = ContentType.objects.get_for_model(TestModel)
    qs_all = TestModel.objects.all()
    cache_key_all = TestModel.objects._get_cache_key(qs_all)

    fc_entry_all = FastCount.objects.get(queryset_hash=cache_key_all)
    assert fc_entry_all.count == 4
    assert fc_entry_all.is_precached is False
    assert fc_entry_all.manager_name == "objects" # Default manager name
    assert cache.get(cache_key_all) == 4

    # Count for .filter(flag=False) is 3, threshold is 2. Should cache.
    cache.delete(cache_key_all) # Clear previous Django cache entry for .all()
    FastCount.objects.all().delete() # Clear DB cache as well for isolation

    assert TestModel.objects.filter(flag=False).count() == 3
    qs_flag_false = TestModel.objects.filter(flag=False)
    cache_key_flag_false = TestModel.objects._get_cache_key(qs_flag_false)
    fc_entry_flag_false = FastCount.objects.get(queryset_hash=cache_key_flag_false)
    assert fc_entry_flag_false.count == 3
    assert fc_entry_flag_false.is_precached is False
    assert cache.get(cache_key_flag_false) == 3
    
    # Count for .filter(flag=True) is 1, threshold is 2. Should NOT cache.
    cache.delete(cache_key_flag_false)
    FastCount.objects.all().delete()

    assert TestModel.objects.filter(flag=True).count() == 1
    assert FastCount.objects.count() == 0
    qs_flag_true = TestModel.objects.filter(flag=True)
    cache_key_flag_true = TestModel.objects._get_cache_key(qs_flag_true)
    assert cache.get(cache_key_flag_true) is None

def test_precache_counts_method_direct_call():
    """Test direct call to manager's precache_counts method."""
    create_test_models_deterministic(flag_true_count=4, flag_false_count=6) # Total 10
    
    manager = TestModel.objects
    results = manager.precache_counts(manager_name="objects")
    
    assert len(results) == 3 # .all(), .filter(flag=True), .filter(flag=False)
    model_ct = ContentType.objects.get_for_model(TestModel)

    # Check .all()
    qs_all = manager.all()
    key_all = manager._get_cache_key(qs_all)
    assert results[key_all] == 10
    fc_all = FastCount.objects.get(queryset_hash=key_all)
    assert fc_all.count == 10
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 10
    
    # Check .filter(flag=True)
    qs_true = manager.filter(flag=True)
    key_true = manager._get_cache_key(qs_true)
    assert results[key_true] == 4
    fc_true = FastCount.objects.get(queryset_hash=key_true)
    assert fc_true.count == 4
    assert fc_true.is_precached is True
    assert cache.get(key_true) == 4

def test_management_command_precache_fast_counts():
    """Test the precache_fast_counts management command."""
    create_test_models_deterministic(flag_true_count=3, flag_false_count=7) # Total 10
    
    call_command("precache_fast_counts")
    
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager = TestModel.objects

    key_all = manager._get_cache_key(manager.all())
    fc_all = FastCount.objects.get(queryset_hash=key_all)
    assert fc_all.count == 10
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 10

    key_true = manager._get_cache_key(manager.filter(flag=True))
    fc_true = FastCount.objects.get(queryset_hash=key_true)
    assert fc_true.count == 3
    assert fc_true.is_precached is True
    assert cache.get(key_true) == 3

def test_count_uses_django_cache(monkeypatch):
    """Test that .count() uses Django's cache if available."""
    create_test_models_deterministic(flag_true_count=5) # Actual count is 5
    
    qs = TestModel.objects.all()
    cache_key = TestModel.objects._get_cache_key(qs)
    cache.set(cache_key, 999, timeout=60) # Manually set Django cache

    monkeypatch.setattr(TestModel.objects, "maybe_trigger_precache", lambda *args, **kwargs: None)

    with patch("django_fast_count.models.FastCount.objects.get") as mock_db_cache_get:
        with patch("django.db.models.query.QuerySet.count") as mock_actual_db_count:
            assert TestModel.objects.count() == 999 # Should use Django cache value
            mock_actual_db_count.assert_not_called()
            mock_db_cache_get.assert_not_called()
    assert cache.get(cache_key) == 999 # Ensure cache entry is still there

def test_count_uses_db_cache(monkeypatch):
    """Test that .count() uses DB cache (FastCount model) if Django cache misses."""
    create_test_models_deterministic(flag_true_count=5) # Actual count is 5
    model_ct = ContentType.objects.get_for_model(TestModel)
    manager = TestModel.objects
    qs = manager.all()
    cache_key = manager._get_cache_key(qs)

    cache.delete(cache_key) # Ensure Django cache is empty

    FastCount.objects.create(
        content_type=model_ct, manager_name="objects", queryset_hash=cache_key,
        count=888, expires_at=timezone.now() + timedelta(minutes=10), is_precached=True
    )
    
    monkeypatch.setattr(TestModel.objects, "maybe_trigger_precache", lambda *args, **kwargs: None)
    
    with patch("django.db.models.query.QuerySet.count") as mock_actual_db_count:
        assert TestModel.objects.count() == 888 # Should use DB cache value
        mock_actual_db_count.assert_not_called() 
    
    assert cache.get(cache_key) == 888 # Django cache should be populated from DB

def test_db_cache_expiration_leads_to_recount(monkeypatch):
    """Test that an expired DB cache entry leads to a recount and re-cache."""
    create_test_models_deterministic(flag_true_count=3) # Actual count is 3
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 2)
    
    assert TestModel.objects.count() == 3 # Trigger retroactive cache

    model_ct = ContentType.objects.get_for_model(TestModel)
    manager = TestModel.objects
    qs = manager.all()
    cache_key = manager._get_cache_key(qs)
    fc_entry = FastCount.objects.get(queryset_hash=cache_key)
    
    # Expire the DB cache entry
    fc_entry.expires_at = timezone.now() - timedelta(seconds=1)
    fc_entry.save()
    cache.delete(cache_key) # Clear Django cache

    # Patch the underlying super().count() to trace its call
    with patch("django.db.models.query.QuerySet.count", return_value=3) as mock_super_count:
        assert TestModel.objects.count() == 3 # Should recount
        mock_super_count.assert_called_once()

    new_fc_entry = FastCount.objects.get(queryset_hash=cache_key)
    assert new_fc_entry.count == 3
    assert new_fc_entry.expires_at > timezone.now() # Should be re-cached
    assert new_fc_entry.is_precached is False # Retroactively

@patch("django.utils.timezone.now")
def test_django_cache_expiration_db_cache_still_valid(mock_now, monkeypatch):
    """Test Django cache expiry when DB cache is still valid."""
    initial_time = timezone.datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_now.return_value = initial_time

    create_test_models_deterministic(flag_true_count=3)
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 2)
    # Manager default expire_cached_counts_after is 1 minute, let's use that.
    # Or set it to be longer for clarity:
    monkeypatch.setattr(TestModel.objects, "expire_cached_counts_after", timedelta(minutes=10))

    assert TestModel.objects.count() == 3 # Populates both caches

    qs = TestModel.objects.all()
    cache_key = TestModel.objects._get_cache_key(qs)
    assert cache.get(cache_key) == 3
    
    # Simulate Django cache expiring (e.g. cleared)
    cache.delete(cache_key)
    
    # Advance time, but DB cache entry (expires_at = initial_time + 10min) is still valid
    mock_now.return_value = initial_time + timedelta(minutes=5)

    monkeypatch.setattr(TestModel.objects, "maybe_trigger_precache", lambda *args, **kwargs: None)
    with patch("django.db.models.query.QuerySet.count") as mock_super_count:
        assert TestModel.objects.count() == 3 # Should hit DB cache
        mock_super_count.assert_not_called()

    assert cache.get(cache_key) == 3 # Django cache repopulated

def test_precache_all_only_if_fast_count_querysets_not_defined(monkeypatch):
    """Test precaching defaults to .all() if fast_count_querysets is not on model."""
    original_method = getattr(TestModel, "fast_count_querysets", None)
    monkeypatch.delattr(TestModel, "fast_count_querysets", raising=False)

    create_test_models_deterministic(flag_true_count=5, flag_false_count=5) # Total 10

    manager = TestModel.objects
    results = manager.precache_counts(manager_name="objects")

    assert len(results) == 1 # Only .all() should be precached
    
    key_all = manager._get_cache_key(manager.all())
    assert results[key_all] == 10
    assert FastCount.objects.get(queryset_hash=key_all).count == 10

    if original_method: # Restore for other tests if needed
        monkeypatch.setattr(TestModel, "fast_count_querysets", original_method)

def test_manager_name_determination_for_default_manager(monkeypatch, capsys):
    """Ensure manager name is correctly determined without warnings for typical setup."""
    create_test_models_deterministic(flag_true_count=1)
    
    # Check that the warning about manager name is not printed
    with patch("builtins.print") as mock_print_builtin: # manager.py uses builtins.print
        TestModel.objects.all().count() 
        for call_args in mock_print_builtin.call_args_list:
            args, _ = call_args
            if args and "Warning: Could not determine manager name" in args[0]:
                pytest.fail(f"Manager name determination warning was printed: {args[0]}")

    # Force caching to check manager_name in DB
    monkeypatch.setattr(TestModel.objects, "cache_counts_larger_than", 0)
    TestModel.objects.all().count()
    model_ct = ContentType.objects.get_for_model(TestModel)
    assert FastCount.objects.filter(content_type=model_ct, manager_name="objects").exists()

def test_get_precache_querysets_handles_misconfigured_instance_method(monkeypatch, capsys):
    """Test warning and fallback if fast_count_querysets is an instance method."""
    original_method = TestModel.fast_count_querysets
    
    def misconfigured_method(self_param): # Defined as an instance method
        return [TestModel.objects.filter(flag=True)] 
    
    # Patch it onto the class
    monkeypatch.setattr(TestModel, "fast_count_querysets", misconfigured_method)

    manager = TestModel.objects
    querysets_to_precache = manager.get_precache_querysets()
    
    # Should fall back to only .all() and print a warning
    assert len(querysets_to_precache) == 1
    # Ensure it's the .all() queryset. Comparing query objects can be tricky.
    # Check if the query SQL string representation is the same.
    expected_all_sql, _ = TestModel.objects.all().query.get_compiler(using=manager.db).as_sql()
    actual_precached_sql, _ = querysets_to_precache[0].query.get_compiler(using=manager.db).as_sql()
    assert actual_precached_sql == expected_all_sql

    captured = capsys.readouterr()
    assert "fast_count_querysets seems to be an instance method" in captured.out
    
    monkeypatch.setattr(TestModel, "fast_count_querysets", original_method) # Restore
