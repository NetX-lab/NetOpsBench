"""Internal session-layer execution contracts and payload references."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netopsbench.platform.scenario.models import Scenario
from netopsbench.platform.scenario.parser import parse_scenario_file, scenario_from_dict, scenario_to_dict


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Explicit per-worker runtime context for session execution without mutating process env."""

    topology_dir: Path
    topology_id: str
    influxdb_bucket: str

    def as_env(self) -> dict[str, str]:
        return {
            "NETOPSBENCH_TOPOLOGY_DIR": str(self.topology_dir),
            "NETOPSBENCH_TOPOLOGY_ID": self.topology_id,
            "NETOPSBENCH_INFLUXDB_BUCKET": self.influxdb_bucket,
        }


@dataclass(frozen=True)
class ScenarioExecutionRef:
    """Pure platform reference object for scenarios scheduled through session orchestration."""

    payload: dict[str, Any]
    path: Path | None = None

    @property
    def id(self) -> str:
        return str(self.payload["scenario_id"])

    @property
    def scale(self) -> str:
        return str(self.payload.get("topology_scale") or "xs")

    def to_scenario(self) -> Scenario:
        return scenario_from_dict(dict(self.payload))

    @classmethod
    def from_scenario(cls, scenario: Scenario, path: Path | None = None) -> ScenarioExecutionRef:
        return cls(payload=scenario_to_dict(scenario), path=path)

    @classmethod
    def from_path(cls, path: str | Path) -> ScenarioExecutionRef:
        resolved = Path(path)
        return cls.from_scenario(parse_scenario_file(resolved), path=resolved)

    @classmethod
    def coerce(cls, value: Any) -> ScenarioExecutionRef:
        if isinstance(value, cls):
            return value
        if isinstance(value, Scenario):
            return cls.from_scenario(value)
        if isinstance(value, (str, Path)):
            return cls.from_path(value)
        to_scenario = getattr(value, "to_scenario", None)
        if callable(to_scenario):
            scenario_obj = to_scenario()
            if not isinstance(scenario_obj, Scenario):
                raise TypeError(f"Expected to_scenario() to return Scenario, got {type(scenario_obj)!r}")
            source_path = getattr(value, "path", None)
            return cls.from_scenario(
                scenario_obj,
                path=Path(source_path) if isinstance(source_path, (str, Path)) else None,
            )
        raise TypeError(f"Unsupported scenario value: {type(value)!r}")


__all__ = ["WorkerExecutionContext", "ScenarioExecutionRef"]
