import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

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


def test_retirement_and_prune_eligibility_require_full_seven_days(tmp_path):
    registry = ManagedBucketRegistry(tmp_path / "ownership.json")
    created = datetime(2026, 7, 1, tzinfo=UTC)
    retired = datetime(2026, 7, 2, tzinfo=UTC)
    registry.record_created("managed", "runtime-xs", now=created)
    registry.retire(["managed", "unowned"], now=retired)

    assert registry.eligible(now=retired + timedelta(days=7) - timedelta(seconds=1)) == []
    assert registry.eligible(now=retired + timedelta(days=7)) == [
        {
            "bucket": "managed",
            "runtime_id": "runtime-xs",
            "retired_at": "2026-07-02T00:00:00Z",
        }
    ]

    registry.mark_deleted("managed", now=retired + timedelta(days=7))
    assert registry.eligible(now=retired + timedelta(days=8)) == []
    assert "managed" not in json.loads(registry.path.read_text(encoding="utf-8"))["buckets"]


def test_recreated_deleted_bucket_starts_a_new_owned_lifecycle(tmp_path):
    path = tmp_path / "ownership.json"
    registry = ManagedBucketRegistry(path)
    first = datetime(2026, 7, 1, tzinfo=UTC)
    second = datetime(2026, 7, 20, tzinfo=UTC)
    registry.record_created("managed", "runtime-old", now=first)
    registry.retire(["managed"], now=first + timedelta(days=1))
    registry.mark_deleted("managed", now=first + timedelta(days=8))

    registry.record_created("managed", "runtime-new", now=second)

    entry = json.loads(path.read_text(encoding="utf-8"))["buckets"]["managed"]
    assert entry == {
        "runtime_id": "runtime-new",
        "created_at": "2026-07-20T00:00:00Z",
        "retired_at": None,
        "deleted_at": None,
    }


def test_runtime_manager_prune_is_dry_run_by_default_and_deletes_only_owned(monkeypatch, tmp_path):
    from netopsbench.platform.runtime import manager as runtime_manager

    manager = runtime_manager.RuntimeManager(workspace=tmp_path)
    registry = ManagedBucketRegistry(manager.telemetry_ownership_file)
    registry.record_created("managed", "runtime-xs", now=datetime(2020, 1, 1, tzinfo=UTC))
    registry.retire(["managed"], now=datetime(2020, 1, 2, tzinfo=UTC))
    deleted = []
    monkeypatch.setattr(
        runtime_manager,
        "delete_bucket",
        lambda _url, _token, bucket: deleted.append(bucket) or True,
    )

    assert [item["bucket"] for item in manager.telemetry_prune()] == ["managed"]
    assert deleted == []

    assert [item["bucket"] for item in manager.telemetry_prune(apply=True)] == ["managed"]
    assert deleted == ["managed"]
    assert manager.telemetry_prune() == []
