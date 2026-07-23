"""Public canonical scenario authoring API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netopsbench.models.profiles import ScaleRegistry, default_scale_registry, supported_scales
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.parser import parse_scenario_file, save_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario


class ScenarioManager:
    """Create, load, validate, and save canonical scenarios."""

    def __init__(self, workspace: str | Path = ".", scale_registry: ScaleRegistry | None = None):
        self.workspace = Path(workspace)
        self.scale_registry = scale_registry or default_scale_registry()

    def create(
        self,
        *,
        id: str,
        name: str,
        episode: EpisodeSpec | dict[str, Any],
        description: str = "",
        scale: str = "xs",
        traffic_profile: str = "standard",
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ScenarioSpec:
        self.scale_registry.get(scale)
        if traffic_profile != "standard":
            raise ValueError(f"Only the standard traffic profile is supported, got: {traffic_profile}")
        episode_spec = episode if isinstance(episode, EpisodeSpec) else EpisodeSpec.model_validate(episode)
        return ScenarioSpec(
            scenario_id=id,
            name=name,
            description=description,
            topology_scale=scale,
            traffic_profile="standard",
            episode=episode_spec,
            metadata=dict(metadata or {}),
            parameters=dict(parameters or {}),
        )

    def load(self, path: str | Path) -> ScenarioSpec:
        scenario = parse_scenario_file(path)
        errors = self.validate(scenario)
        if errors:
            raise ValueError("Invalid scenario: " + "; ".join(errors))
        return scenario

    def save(self, scenario: ScenarioSpec, path: str | Path) -> Path:
        return save_scenario_file(scenario, path)

    def validate(self, scenario: ScenarioSpec) -> list[str]:
        fault_manager = getattr(getattr(self, "platform", None), "faults", None)
        registry = getattr(fault_manager, "spec_registry", None)
        return validate_scenario(
            scenario,
            fault_registry=registry,
            scale_registry=self.scale_registry,
        )

__all__ = ["EpisodeSpec", "ScenarioManager", "ScenarioSpec", "supported_scales"]
