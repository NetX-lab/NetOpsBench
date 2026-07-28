"""Public artifact path and metadata helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from netopsbench.platform.session.harbor_export import export_traces, load_trace_index, load_trace_results
from netopsbench.platform.utils.files import atomic_write_json


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

    def get_run_traces_dir(self, run_id: str) -> Path:
        return self.get_run_dir(run_id) / "traces"

    def get_runtime_metadata_path(self, runtime_id: str) -> Path:
        return self.get_runtime_dir(runtime_id) / "metadata.json"

    def save_metadata(self, target_dir: Path, payload: dict[str, Any]) -> Path:
        path = Path(target_dir)
        path.mkdir(parents=True, exist_ok=True)
        metadata_path = path / "metadata.json"
        atomic_write_json(metadata_path, payload)
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

    def get_run_traces(self, run_id: str) -> list[dict[str, Any]]:
        """Return the run-level trace index rows for ``run_id``."""

        return load_trace_index(self.get_run_dir(run_id))

    def get_run_trace_results(self, run_id: str) -> list[dict[str, Any]]:
        """Return run-level trace scoring/result rows for ``run_id``."""

        return load_trace_results(self.get_run_dir(run_id))

    def export_traces(self, run_id: str, *, output: str | Path) -> Path:
        """Export run traces as a Harbor jobs directory."""

        return export_traces(self.get_run_dir(run_id), output=output)
