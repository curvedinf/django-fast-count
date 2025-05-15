import pytest
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from unittest.mock import patch, ANY
from testapp.models import ModelWithIntermediateManager, ModelWithDeepManager
from django_fast_count.models import FastCount
from django_fast_count.managers import FastCountQuerySet, FastCountManager

# Pytest marker for DB access for all tests in this module
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_caches_and_fastcount_db_for_each_test():
    """Ensures a clean state for each test."""
    cache.clear()
    FastCount.objects.all().delete()
    ModelWithIntermediateManager.objects.all().delete()
    ModelWithDeepManager.objects.all().delete()
    yield
    cache.clear()
    FastCount.objects.all().delete()
    ModelWithIntermediateManager.objects.all().delete()
    ModelWithDeepManager.objects.all().delete()


def create_intermediate_models(count=0):
    """Helper to create ModelWithIntermediateManager instances."""
    ModelWithIntermediateManager.objects.bulk_create(
        [ModelWithIntermediateManager(field=f"test_{i}") for i in range(count)]
    )
    return count


def create_deep_models(gt_5_count=0, lte_5_count=0):
    """Helper to create ModelWithDeepManager instances."""
    ModelWithDeepManager.objects.bulk_create(
        [ModelWithDeepManager(another_field=i + 6) for i in range(gt_5_count)]
    )
    ModelWithDeepManager.objects.bulk_create(
        [ModelWithDeepManager(another_field=i) for i in range(lte_5_count)]
    )
    return gt_5_count + lte_5_count


# --- Tests for ModelWithIntermediateManager ---
def test_intermediate_initial_count_no_cache():
    """Test basic counting for IntermediateManager without caching triggered."""
    create_intermediate_models(count=5)
    # Default cache_counts_larger_than is 1_000_000 for this manager
    assert ModelWithIntermediateManager.objects.count() == 5
    assert FastCount.objects.count() == 0
    with patch("django.core.cache.cache.set") as mock_cache_set:
        ModelWithIntermediateManager.objects.count()
        mock_cache_set.assert_not_called()


def test_intermediate_retroactive_caching(monkeypatch):
    """Test retroactive caching for IntermediateManager."""
    # Default cache_counts_larger_than is 1M, override for test
    monkeypatch.setattr(ModelWithIntermediateManager.objects, "cache_counts_larger_than", 2)
    create_intermediate_models(count=3)
    model_ct = ContentType.objects.get_for_model(ModelWithIntermediateManager)
    manager_name_expected = "objects"
    assert ModelWithIntermediateManager.objects.count() == 3
    qs_all = ModelWithIntermediateManager.objects.all()
    cache_key_all = qs_all._get_cache_key()
    fc_entry_all = FastCount.objects.get(
        content_type=model_ct,
        manager_name=manager_name_expected,
        queryset_hash=cache_key_all,
    )
    assert fc_entry_all.count == 3
    assert fc_entry_all.is_precached is False
    assert fc_entry_all.manager_name == manager_name_expected
    assert cache.get(cache_key_all) == 3


def test_intermediate_precache_counts_direct_call():
    """Test direct call to IntermediateManager's precache_counts."""
    create_intermediate_models(count=7)
    manager = ModelWithIntermediateManager.objects
    manager_name_expected = "objects"
    qs_for_precache = manager.all() # This is an IntermediateFastCountQuerySet
    assert isinstance(qs_for_precache, FastCountQuerySet) # Check it's our QS
    assert hasattr(qs_for_precache, "precache_counts")
    results = qs_for_precache.precache_counts()
    assert len(results) == 1  # Only .all() from fast_count_querysets
    model_ct = ContentType.objects.get_for_model(ModelWithIntermediateManager)
    qs_all = manager.all()
    key_all = qs_all._get_cache_key()
    assert results[key_all] == 7
    fc_all = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_all
    )
    assert fc_all.count == 7
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 7


def test_intermediate_management_command_precache():
    """Test precache_fast_counts command for IntermediateManager."""
    create_intermediate_models(count=4)
    call_command("precache_fast_counts")
    model_ct = ContentType.objects.get_for_model(ModelWithIntermediateManager)
    manager = ModelWithIntermediateManager.objects
    manager_name_expected = "objects"
    qs_all_for_key = manager.all()
    key_all = qs_all_for_key._get_cache_key()
    fc_all = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_all
    )
    assert fc_all.count == 4
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 4


# --- Tests for ModelWithDeepManager ---
def test_deep_initial_count_no_cache():
    """Test basic counting for DeepManager without caching triggered."""
    create_deep_models(gt_5_count=2, lte_5_count=3) # Total 5
    # Default cache_counts_larger_than is 1_000_000 for this manager
    assert ModelWithDeepManager.objects.count() == 5
    assert ModelWithDeepManager.objects.filter(another_field__gt=5).count() == 2  # (6,7) from gt_5_count=2; (0,1,2) from lte_5_count=3. gt=5 -> 2 items
    assert ModelWithDeepManager.objects.filter(another_field__lte=5).count() == 3 # lte=5 -> 3 items
    assert FastCount.objects.count() == 0
    with patch("django.core.cache.cache.set") as mock_cache_set:
        ModelWithDeepManager.objects.count()
        ModelWithDeepManager.objects.filter(another_field__gt=5).count()
        mock_cache_set.assert_not_called()


def test_deep_retroactive_caching_all(monkeypatch):
    """Test retroactive caching for DeepManager on .all()."""
    monkeypatch.setattr(ModelWithDeepManager.objects, "cache_counts_larger_than", 2)
    create_deep_models(gt_5_count=1, lte_5_count=3)  # Total 4
    model_ct = ContentType.objects.get_for_model(ModelWithDeepManager)
    manager_name_expected = "objects"
    assert ModelWithDeepManager.objects.count() == 4 # Triggers retroactive cache for .all()
    qs_all = ModelWithDeepManager.objects.all()
    cache_key_all = qs_all._get_cache_key()
    fc_entry_all = FastCount.objects.get(
        content_type=model_ct,
        manager_name=manager_name_expected,
        queryset_hash=cache_key_all,
    )
    assert fc_entry_all.count == 4
    assert fc_entry_all.is_precached is False
    assert cache.get(cache_key_all) == 4


def test_deep_retroactive_caching_filter(monkeypatch):
    """Test retroactive caching for DeepManager on a filter."""
    monkeypatch.setattr(ModelWithDeepManager.objects, "cache_counts_larger_than", 1)
    # gt_5_count=2 -> another_field values 6, 7
    # lte_5_count=3 -> another_field values 0, 1, 2
    # .filter(another_field__gt=5) should yield count = 2 (from 6, 7)
    create_deep_models(gt_5_count=2, lte_5_count=3)
    model_ct = ContentType.objects.get_for_model(ModelWithDeepManager)
    manager_name_expected = "objects"

    # Test .filter(another_field__gt=5)
    assert ModelWithDeepManager.objects.filter(another_field__gt=5).count() == 2
    qs_gt5 = ModelWithDeepManager.objects.filter(another_field__gt=5)
    cache_key_gt5 = qs_gt5._get_cache_key()
    fc_entry_gt5 = FastCount.objects.get(
        content_type=model_ct,
        manager_name=manager_name_expected,
        queryset_hash=cache_key_gt5,
    )
    assert fc_entry_gt5.count == 2
    assert fc_entry_gt5.is_precached is False
    assert cache.get(cache_key_gt5) == 2


def test_deep_precache_counts_direct_call():
    """Test direct call to DeepManager's precache_counts."""
    # gt_5_count=4 -> fields 6,7,8,9 (4 items > 5)
    # lte_5_count=6 -> fields 0,1,2,3,4,5 (6 items <= 5)
    # Total 10.
    # filter(another_field__gt=5) should be 4 items.
    # filter(another_field__lte=5) should be 6 items.
    create_deep_models(gt_5_count=4, lte_5_count=6)
    manager = ModelWithDeepManager.objects
    manager_name_expected = "objects"
    qs_for_precache = manager.all() # This is a DeepFastCountQuerySet
    assert isinstance(qs_for_precache, FastCountQuerySet)
    results = qs_for_precache.precache_counts()

    # .all(), .filter(another_field__gt=5), .filter(another_field__lte=5)
    assert len(results) == 3
    model_ct = ContentType.objects.get_for_model(ModelWithDeepManager)

    # Check .all()
    qs_all = manager.all()
    key_all = qs_all._get_cache_key()
    assert results[key_all] == 10
    fc_all = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_all
    )
    assert fc_all.count == 10
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 10

    # Check .filter(another_field__gt=5)
    qs_gt5 = manager.filter(another_field__gt=5)
    key_gt5 = qs_gt5._get_cache_key()
    assert results[key_gt5] == 4
    fc_gt5 = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_gt5
    )
    assert fc_gt5.count == 4
    assert fc_gt5.is_precached is True
    assert cache.get(key_gt5) == 4

    # Check .filter(another_field__lte=5)
    qs_lte5 = manager.filter(another_field__lte=5)
    key_lte5 = qs_lte5._get_cache_key()
    assert results[key_lte5] == 6
    fc_lte5 = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_lte5
    )
    assert fc_lte5.count == 6
    assert fc_lte5.is_precached is True
    assert cache.get(key_lte5) == 6


def test_deep_management_command_precache():
    """Test precache_fast_counts command for DeepManager."""
    # gt_5_count=3 -> another_field: 6, 7, 8 (all > 5)
    # lte_5_count=7 -> another_field: 0, 1, 2, 3, 4, 5, 6
    # Total = 10
    # filter(another_field__gt=5) -> 6, 7, 8 (from first batch) + 6 (from second batch) = 4 items
    # filter(another_field__lte=5) -> 0, 1, 2, 3, 4, 5 (from second batch) = 6 items
    create_deep_models(gt_5_count=3, lte_5_count=7)
    call_command("precache_fast_counts")

    model_ct = ContentType.objects.get_for_model(ModelWithDeepManager)
    manager = ModelWithDeepManager.objects
    manager_name_expected = "objects"

    # Check .all()
    qs_all = manager.all()
    key_all = qs_all._get_cache_key()
    fc_all = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_all
    )
    assert fc_all.count == 10
    assert fc_all.is_precached is True
    assert cache.get(key_all) == 10

    # Check .filter(another_field__gt=5)
    qs_gt5 = manager.filter(another_field__gt=5)
    key_gt5 = qs_gt5._get_cache_key()
    fc_gt5 = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_gt5
    )
    assert fc_gt5.count == 4
    assert fc_gt5.is_precached is True
    assert cache.get(key_gt5) == 4

    # Check .filter(another_field__lte=5)
    qs_lte5 = manager.filter(another_field__lte=5)
    key_lte5 = qs_lte5._get_cache_key()
    fc_lte5 = FastCount.objects.get(
        content_type=model_ct, manager_name=manager_name_expected, queryset_hash=key_lte5
    )
    assert fc_lte5.count == 6
    assert fc_lte5.is_precached is True
    assert cache.get(key_lte5) == 6


def test_intermediate_queryset_type():
    """Ensure manager returns the correct custom queryset type."""
    qs = ModelWithIntermediateManager.objects.all()
    # Check if it's an instance of FastCountQuerySet, but more specifically
    # it should be IntermediateFastCountQuerySet or a subclass thereof.
    # The actual class is IntermediateFastCountQuerySet from managers.py
    from testapp.managers import IntermediateFastCountQuerySet
    assert isinstance(qs, IntermediateFastCountQuerySet)
    assert qs.manager_name == "objects" # check manager_name propagated


def test_deep_queryset_type():
    """Ensure manager returns the correct custom queryset type."""
    qs = ModelWithDeepManager.objects.all()
    # The actual class is DeepFastCountQuerySet from managers.py
    from testapp.managers import DeepFastCountQuerySet
    assert isinstance(qs, DeepFastCountQuerySet)
    assert qs.manager_name == "objects" # check manager_name propagated