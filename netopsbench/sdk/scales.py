"""Public scale profile registry access."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from netopsbench.models.profiles import ScaleProfile, ScaleRegistry


class ScaleManager:
    """Expose the resolved scale registry owned by one benchmark instance."""

    def __init__(self, profile_files: Iterable[str | Path] = ()):
        self.registry = ScaleRegistry.with_builtins(tuple(profile_files))

    @property
    def digest(self) -> str:
        return self.registry.digest

    def get(self, name: str) -> ScaleProfile:
        return self.registry.get(name)

    def names(self) -> tuple[str, ...]:
        return self.registry.names()

    def resolved(self) -> list[dict[str, object]]:
        return [profile.model_dump(mode="json") for profile in self.registry.values()]


__all__ = ["ScaleManager", "ScaleProfile", "ScaleRegistry"]
