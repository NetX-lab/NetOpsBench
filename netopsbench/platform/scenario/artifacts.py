"""Persist large private route evidence separately from the public result fields."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from netopsbench.platform.utils.files import atomic_write_bytes, atomic_write_json


def persist_result(path: Path, result: dict) -> None:
    """Keep results in JSON and losslessly compress the private phase snapshots."""
    evidence_dir = path.parent / "evidence" / path.stem

    def snapshot_reference(name, snapshot):
        raw = json.dumps(snapshot, separators=(",", ":"), default=str, ensure_ascii=False).encode()
        compressed = gzip.compress(raw, compresslevel=3, mtime=0)
        artifact = evidence_dir / f"{name}.json.gz"
        atomic_write_bytes(artifact, compressed)
        return {
            "path": str(artifact.resolve()),
            "encoding": "gzip+json",
            "sha256": hashlib.sha256(compressed).hexdigest(),
            "collected_at": snapshot.get("collected_at"),
            "uncompressed_bytes": len(raw),
            "compressed_bytes": len(compressed),
        }

    # Shallow-copy only the serialized branches: execution, scoring and the
    # checkpoint callbacks must still receive the original live observations.
    payload = dict(result)
    episode = result.get("episode") or {}
    if episode.get("route_evidence"):
        payload["episode"] = {
            **episode,
            "route_evidence": {
                phase: snapshot_reference(phase, snapshot) for phase, snapshot in episode["route_evidence"].items()
            },
        }
    cleanup = result.get("cleanup") or {}
    if cleanup.get("route_recovery"):
        payload["cleanup"] = {**cleanup, "route_recovery": snapshot_reference("recovery", cleanup["route_recovery"])}
    atomic_write_json(path, payload, default=str)
