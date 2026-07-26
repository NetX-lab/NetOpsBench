"""Workspace-local ownership records for NetOpsBench-created telemetry buckets."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netopsbench.platform.utils.files import atomic_write_json


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ManagedBucketRegistry:
    """Track only buckets whose creation was confirmed by this workspace."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "2", "buckets": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "2" or not isinstance(payload.get("buckets"), dict):
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
            if existing is not None:
                return
            buckets[bucket] = {
                "runtime_id": runtime_id,
                "created_at": _timestamp(now or datetime.now(UTC)),
            }
            self._save(payload)

    def active_for_runtime(self, runtime_id: str) -> list[str]:
        """Return active buckets created for one runtime by this workspace."""
        with self._locked():
            return sorted(
                bucket for bucket, entry in self._load()["buckets"].items() if entry.get("runtime_id") == runtime_id
            )

    def mark_deleted(self, bucket: str) -> None:
        with self._locked():
            payload = self._load()
            if bucket not in payload["buckets"]:
                return
            del payload["buckets"][bucket]
            self._save(payload)


__all__ = ["ManagedBucketRegistry"]
