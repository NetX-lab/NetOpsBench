"""Canonical identity for a runtime worker and its isolated resources."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def safe_runtime_label(value: str) -> str:
    """Return a bucket-safe runtime label compatible with existing worker labels."""
    text = (value or "unknown").strip().lower()
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text) or "unknown"


def _default_topology_id(data: dict[str, Any]) -> str:
    return str(data["lab_name"])


def _default_bucket(data: dict[str, Any]) -> str:
    return f"network_data_{safe_runtime_label(str(data['runtime_id']))}_w{int(data['worker_index']):02d}"


class RuntimeIdentity(BaseModel):
    """Persisted worker identity with deterministic resource names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["4"] = "4"
    runtime_id: str
    worker_id: str
    worker_index: int = Field(ge=1)
    lab_name: str
    topology_id: str = Field(default_factory=_default_topology_id)
    topology_dir: Path
    bucket: str = Field(default_factory=_default_bucket)
    mgmt_subnet: str
    mgmt_network: str

    @classmethod
    def create(
        cls,
        *,
        runtime_id: str,
        worker_id: str,
        worker_index: int,
        lab_name: str,
        topology_dir: str | Path,
        mgmt_subnet: str,
        mgmt_network: str,
        topology_id: str | None = None,
        bucket: str | None = None,
    ) -> RuntimeIdentity:
        """Create an identity with deterministic topology and bucket defaults."""
        return cls(
            runtime_id=runtime_id,
            worker_id=worker_id,
            worker_index=worker_index,
            lab_name=lab_name,
            topology_id=topology_id if topology_id is not None else lab_name,
            topology_dir=Path(topology_dir),
            bucket=bucket or f"network_data_{safe_runtime_label(runtime_id)}_w{worker_index:02d}",
            mgmt_subnet=mgmt_subnet,
            mgmt_network=mgmt_network,
        )


__all__ = ["RuntimeIdentity", "safe_runtime_label"]
