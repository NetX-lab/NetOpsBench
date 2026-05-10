"""Scenario data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Episode:
    """Single episode in a scenario: deploy → traffic → fault → observe → recover."""

    episode_id: str
    description: str
    fault_type: str
    target_device: str | None = None
    target_interface: str | None = None
    target_prefix: str | None = None
    mtu: int | None = None
    duration_seconds: int = 30
    stabilization_time: int = 10
    metadata: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)


@dataclass
class Scenario:
    """Complete test scenario with multiple episodes."""

    scenario_id: str
    name: str
    description: str
    topology_scale: str  # xs, small, medium, large
    traffic_profile: str  # light, standard, stress
    episodes: list[Episode]
    metadata: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)


__all__ = ["Episode", "Scenario"]
