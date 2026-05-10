"""Public artifact path and metadata helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ArtifactManager:
    """Thin helper for resolving and persisting public artifact data."""

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace)
        self.root_dir = self.workspace / ".netopsbench"

    def get_run_dir(self, run_id: str) -> Path:
        return self.root_dir / "runs" / str(run_id)

    def get_runtime_dir(self, runtime_id: str) -> Path:
        return self.root_dir / "runtimes" / str(runtime_id)

    def get_run_metadata_path(self, run_id: str) -> Path:
        return self.get_run_dir(run_id) / "metadata.json"

    def get_runtime_metadata_path(self, runtime_id: str) -> Path:
        return self.get_runtime_dir(runtime_id) / "metadata.json"

    def save_metadata(self, target_dir: Path, payload: dict[str, Any]) -> Path:
        path = Path(target_dir)
        path.mkdir(parents=True, exist_ok=True)
        metadata_path = path / "metadata.json"
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return metadata_path

    def load_metadata(self, target_dir: Path) -> dict[str, Any]:
        metadata_path = Path(target_dir) / "metadata.json"
        if not metadata_path.exists():
            raise FileNotFoundError(f"metadata file not found: {metadata_path}")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid metadata JSON: {metadata_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Metadata payload must be a JSON object: {metadata_path}")
        return payload
