"""Workspace-local ownership records for NetOpsBench-created telemetry buckets."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from netopsbench.platform.utils.files import atomic_write_json

MANAGED_BUCKET_RETIREMENT_AGE = timedelta(days=7)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ManagedBucketRegistry:
    """Track only buckets whose creation was confirmed by this workspace."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1", "buckets": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "1" or not isinstance(payload.get("buckets"), dict):
            raise ValueError(f"Invalid managed telemetry ownership manifest: {self.path}")
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.path, payload)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def record_created(self, bucket: str, runtime_id: str, *, now: datetime | None = None) -> None:
        with self._locked():
            payload = self._load()
            buckets = payload["buckets"]
            existing = buckets.get(bucket)
            if existing is not None and not existing.get("deleted_at"):
                return
            buckets[bucket] = {
                "runtime_id": runtime_id,
                "created_at": _timestamp(now or datetime.now(UTC)),
                "retired_at": None,
                "deleted_at": None,
            }
            self._save(payload)

    def retire(self, buckets: list[str], *, now: datetime | None = None) -> list[str]:
        with self._locked():
            payload = self._load()
            retired: list[str] = []
            retired_at = _timestamp(now or datetime.now(UTC))
            for bucket in buckets:
                entry = payload["buckets"].get(bucket)
                if entry is None or entry.get("retired_at") or entry.get("deleted_at"):
                    continue
                entry["retired_at"] = retired_at
                retired.append(bucket)
            if retired:
                self._save(payload)
            return retired

    def eligible(self, *, now: datetime | None = None) -> list[dict[str, str]]:
        with self._locked():
            cutoff = (now or datetime.now(UTC)) - MANAGED_BUCKET_RETIREMENT_AGE
            eligible: list[dict[str, str]] = []
            for bucket, entry in self._load()["buckets"].items():
                retired_at = entry.get("retired_at")
                if not retired_at or entry.get("deleted_at"):
                    continue
                retired_time = datetime.fromisoformat(str(retired_at).replace("Z", "+00:00"))
                if retired_time <= cutoff:
                    eligible.append(
                        {
                            "bucket": bucket,
                            "runtime_id": str(entry["runtime_id"]),
                            "retired_at": str(retired_at),
                        }
                    )
            return sorted(eligible, key=lambda item: item["bucket"])

    def mark_deleted(self, bucket: str, *, now: datetime | None = None) -> None:
        del now
        with self._locked():
            payload = self._load()
            if bucket not in payload["buckets"]:
                return
            del payload["buckets"][bucket]
            self._save(payload)


__all__ = ["MANAGED_BUCKET_RETIREMENT_AGE", "ManagedBucketRegistry"]
