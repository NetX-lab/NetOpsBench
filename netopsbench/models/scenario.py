"""Canonical scenario models shared by benchmark sessions and simulators."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EpisodeSpec(BaseModel):
    """The single diagnosable episode contained by a scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(min_length=1)
    description: str = ""
    fault_type: str = Field(default="none", min_length=1)
    target_device: str | None = None
    target_interface: str | None = None
    target_prefix: str | None = None
    mtu: int | None = Field(default=None, gt=0)
    duration_seconds: int = Field(default=30, gt=0)
    stabilization_time: int = Field(default=10, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.fault_type == "none"


class ScenarioSpec(BaseModel):
    """A benchmark task with one fault or healthy diagnostic episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    scenario_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    topology_scale: str = Field(default="xs", min_length=1)
    traffic_profile: Literal["standard"] = "standard"
    episode: EpisodeSpec
    metadata: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.scenario_id

    @property
    def scale(self) -> str:
        return self.topology_scale

    @property
    def episodes(self) -> list[EpisodeSpec]:
        """Compatibility view for the pre-canonical public scenario handle."""
        return [self.episode]

    def to_scenario(self) -> ScenarioSpec:
        return self

    def to_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        payload["episodes"] = [payload.pop("episode")]
        return payload

    @classmethod
    def from_scenario(cls, scenario: ScenarioSpec, path: str | None = None) -> ScenarioSpec:
        del path
        return scenario

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["EpisodeSpec", "ScenarioSpec"]
