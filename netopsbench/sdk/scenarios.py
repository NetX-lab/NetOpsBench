"""Public scenario authoring API."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netopsbench.platform.scenario.executor import Episode, Scenario
from netopsbench.platform.scenario.parser import (
    episode_from_dict,
    episode_to_dict,
    parse_scenario_file,
    save_scenario_file,
    scenario_from_dict,
    scenario_to_dict,
)
from netopsbench.platform.scenario.validator import validate_scenario


@dataclass
class ScenarioHandle:
    """Public scenario wrapper storing public-facing data only."""

    _data: dict[str, Any]
    path: Path | None = None

    @property
    def id(self) -> str:
        return self._data["scenario_id"]

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def description(self) -> str:
        return self._data.get("description", "")

    @property
    def scale(self) -> str:
        return self._data.get("topology_scale", "xs")

    @property
    def traffic_profile(self) -> str:
        return self._data.get("traffic_profile", "standard")

    @property
    def metadata(self) -> dict[str, Any]:
        return deepcopy(self._data.get("metadata", {}))

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(self._data.get("parameters", {}))

    @property
    def episodes(self) -> list[dict[str, Any]]:
        return deepcopy(self._data.get("episodes", []))

    def to_scenario(self) -> Scenario:
        return scenario_from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @classmethod
    def from_scenario(cls, scenario: Scenario, path: str | Path | None = None) -> ScenarioHandle:
        return cls(_data=scenario_to_dict(scenario), path=Path(path) if path is not None else None)


class ScenarioManager:
    """Thin adapter over the existing scenario parser helpers."""

    def __init__(self, workspace: str | Path = "."):
        self.workspace = Path(workspace)

    def create(
        self,
        *,
        id: str,
        name: str,
        description: str = "",
        scale: str = "xs",
        traffic_profile: str = "standard",
        episodes: Sequence[dict[str, Any] | Episode] | None = None,
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ScenarioHandle:
        payload = {
            "scenario_id": id,
            "name": name,
            "description": description,
            "topology_scale": scale,
            "traffic_profile": traffic_profile,
            "episodes": [self._coerce_episode_data(item) for item in (episodes or [])],
            "metadata": dict(metadata or {}),
            "parameters": dict(parameters or {}),
        }
        return ScenarioHandle(_data=payload)

    def load(self, path: str | Path) -> ScenarioHandle:
        resolved = Path(path)
        return ScenarioHandle.from_scenario(parse_scenario_file(resolved), path=resolved)

    def save(self, handle: ScenarioHandle | Scenario, path: str | Path) -> Path:
        scenario = self._coerce_scenario(handle)
        return save_scenario_file(scenario, path)

    def validate(self, handle: ScenarioHandle | Scenario) -> list[str]:
        scenario = self._coerce_scenario(handle)
        return validate_scenario(scenario)

    def _coerce_scenario(self, handle: ScenarioHandle | Scenario) -> Scenario:
        if isinstance(handle, ScenarioHandle):
            return handle.to_scenario()
        if isinstance(handle, Scenario):
            return handle
        raise TypeError(f"Unsupported scenario value: {type(handle)!r}")

    def _coerce_episode_data(self, value: dict[str, Any] | Episode) -> dict[str, Any]:
        if isinstance(value, Episode):
            return episode_to_dict(value)
        if isinstance(value, dict):
            return episode_to_dict(episode_from_dict(value))
        raise TypeError(f"Unsupported episode value: {type(value)!r}")
