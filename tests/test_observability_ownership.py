import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from netopsbench.platform.observability.ownership import ManagedBucketRegistry


def test_registry_tracks_only_confirmed_created_buckets_and_writes_atomically(tmp_path):
    path = tmp_path / ".netopsbench" / "telemetry-buckets.json"
    registry = ManagedBucketRegistry(path)
    created = datetime(2026, 7, 1, tzinfo=UTC)

    registry.record_created("managed", "runtime-xs", now=created)
    registry.record_created("managed", "other-runtime", now=created + timedelta(days=1))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["buckets"]) == {"managed"}
    assert payload["buckets"]["managed"]["runtime_id"] == "runtime-xs"
    assert not list(path.parent.glob("*.tmp"))


def test_concurrent_bucket_records_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / ".netopsbench" / "telemetry-buckets.json"
    registry = ManagedBucketRegistry(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: registry.record_created(f"bucket-{index}", "runtime"), range(8)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["buckets"]) == {f"bucket-{index}" for index in range(8)}


def test_registry_rejects_retirement_manifest_from_pre_02_lifecycle(tmp_path):
    path = tmp_path / "ownership.json"
    path.write_text('{"schema_version":"1","buckets":{}}', encoding="utf-8")

    registry = ManagedBucketRegistry(path)

    with pytest.raises(ValueError, match="Invalid managed telemetry ownership manifest"):
        registry.active_for_runtime("runtime")


def test_recreated_deleted_bucket_starts_a_new_owned_lifecycle(tmp_path):
    path = tmp_path / "ownership.json"
    registry = ManagedBucketRegistry(path)
    first = datetime(2026, 7, 1, tzinfo=UTC)
    second = datetime(2026, 7, 20, tzinfo=UTC)
    registry.record_created("managed", "runtime-old", now=first)
    registry.mark_deleted("managed")

    registry.record_created("managed", "runtime-new", now=second)

    entry = json.loads(path.read_text(encoding="utf-8"))["buckets"]["managed"]
    assert entry == {
        "runtime_id": "runtime-new",
        "created_at": "2026-07-20T00:00:00Z",
    }


def test_active_for_runtime_excludes_other_runtime_buckets(tmp_path):
    registry = ManagedBucketRegistry(tmp_path / "ownership.json")
    registry.record_created("active-a", "runtime-a")
    registry.record_created("active-b", "runtime-b")

    assert registry.active_for_runtime("runtime-a") == ["active-a"]
