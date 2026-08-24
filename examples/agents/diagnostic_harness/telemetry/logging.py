"""Write compact, ground-truth-free per-case JSON traces."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from netopsbench.sdk.agents import DiagnosisResult


def _jsonable(value: Any) -> Any:
    if isinstance(value, DiagnosisResult):
        return {
            "agent_name": value.agent_name,
            "verdict": value.verdict,
            "findings": value.findings,
            "confidence": value.confidence,
            "reasoning": value.reasoning,
            "metadata": value.metadata,
        }
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


class CaseTraceWriter:
    def __init__(self, output_directory: str | Path):
        self.output_directory = Path(output_directory)

    def write(self, *, context: Any, payload: dict[str, Any]) -> Path:
        metadata = getattr(context, "metadata", {}) or {}
        runtime_id = metadata.get("runtime_id") or metadata.get("topology_id")
        if not runtime_id:
            worker_env = metadata.get("worker_env") if isinstance(metadata, dict) else None
            topology_dir = worker_env.get("NETOPSBENCH_TOPOLOGY_DIR") if isinstance(worker_env, dict) else None
            if topology_dir:
                path = Path(str(topology_dir))
                runtime_id = path.parent.name if path.name.startswith("worker-") else path.name
        runtime_id = str(runtime_id or "standalone")
        target = self.output_directory / runtime_id / f"trace-{uuid4().hex}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(_jsonable(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return target


__all__ = ["CaseTraceWriter"]
