"""Centralized fault extension metadata and dispatch registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import cache
from types import MappingProxyType
from typing import Any

from .models import FaultExecutor as FaultExecutor
from .models import FaultPack as FaultPack
from .models import FaultSpec as FaultSpec

_CANONICAL_FAULT_ALIASES: dict[str, str] = {
    "static_route_misconfiguration": "static_route_misconfig",
}


# ---------------------------------------------------------------------------
# FaultSpecRegistry — encapsulates all mutable spec state
# ---------------------------------------------------------------------------


class FaultSpecRegistry:
    """Registry holding fault specs, aliases, and schema defaults.

    Each runtime owns an instance so custom faults cannot leak across sessions.
    """

    def __init__(
        self,
        *,
        canonical_aliases: dict[str, str] | None = None,
        schema_defaults: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._specs: dict[str, FaultSpec] = {}
        self._aliases: dict[str, str] = dict(canonical_aliases or _CANONICAL_FAULT_ALIASES)
        self._schema_defaults: dict[str, dict[str, Any]] = dict(
            schema_defaults if schema_defaults is not None else {"blackhole_route": {"requires_prefix": True}}
        )
        self._builtin_names: tuple[str, ...] | None = None

    # -- core operations ---------------------------------------------------

    def canonicalize(self, name: str | None) -> str:
        raw_name = str(name or "").strip()
        if not raw_name:
            return ""
        return self._aliases.get(raw_name, raw_name)

    def register(self, spec: FaultSpec) -> FaultSpec:
        canonical_name = self.canonicalize(spec.name) or spec.name
        defaults = self._schema_defaults.get(canonical_name, {})
        values = {key: value for key, value in defaults.items() if getattr(spec, key) in (False, None, (), [])}
        normalized_aliases = sorted(
            {
                alias
                for alias in ([spec.name] + list(spec.aliases) + [canonical_name])
                if alias and alias != canonical_name
            }
        )
        normalized = replace(
            spec,
            name=canonical_name,
            aliases=normalized_aliases,
            **values,
        )
        self._specs[canonical_name] = normalized
        self._aliases[canonical_name] = canonical_name
        for alias in normalized_aliases:
            self._aliases[alias] = canonical_name
        return normalized

    def get(self, name: str) -> FaultSpec | None:
        self.load_builtins()
        return self._specs.get(self.canonicalize(name))

    def list_all(self) -> list[FaultSpec]:
        self.load_builtins()
        return [spec for _, spec in sorted(self._specs.items(), key=lambda item: item[0])]

    def supported_scenario_faults(self) -> list[str]:
        return [spec.name for spec in self.list_all() if spec.scenario_supported]

    # -- builtin loading ---------------------------------------------------

    def load_builtins(self) -> tuple[str, ...]:
        """Register builtin specs (idempotent). Returns canonical names."""
        if self._builtin_names is None:
            for spec in _build_builtin_fault_specs():
                self.register(spec)
            self._builtin_names = tuple(sorted(self._specs.keys()))
        return self._builtin_names

    def get_builtin_specs(self) -> list[FaultSpec]:
        """Return the builtin :class:`FaultSpec` instances (loading on demand)."""
        builtin_names = self.load_builtins()
        return [self._specs[name] for name in builtin_names if name in self._specs]


# ---------------------------------------------------------------------------
# Builtin spec aggregation (formerly netopsbench.platform.faults.builtin_specs)
# ---------------------------------------------------------------------------


def _build_builtin_fault_specs() -> list[FaultSpec]:
    """Aggregate every builtin spec builder into a single list.

    Imported lazily inside the function to avoid an import cycle:
    ``builtin/__init__`` may transitively import ``specs`` for shared types.
    """
    from .builtin.catalog_specs import build_catalog_fault_specs
    from .builtin.impairment_specs import build_impairment_fault_specs
    from .builtin.link_specs import build_link_fault_specs
    from .builtin.routing_specs import build_routing_fault_specs
    from .builtin.system_specs import build_system_fault_specs

    builders: tuple[Callable[[], list[FaultSpec]], ...] = (
        build_link_fault_specs,
        build_routing_fault_specs,
        build_impairment_fault_specs,
        build_system_fault_specs,
        build_catalog_fault_specs,
    )
    aggregated: list[FaultSpec] = []
    for build_specs in builders:
        aggregated.extend(build_specs())
    return aggregated


def create_fault_registry() -> FaultSpecRegistry:
    """Return an isolated registry populated with builtin fault specs."""
    registry = FaultSpecRegistry()
    registry.load_builtins()
    return registry


@cache
def _builtin_alias_index() -> MappingProxyType:
    aliases = dict(_CANONICAL_FAULT_ALIASES)
    for spec in _build_builtin_fault_specs():
        canonical = aliases.get(spec.name, spec.name)
        aliases[canonical] = canonical
        aliases[spec.name] = canonical
        aliases.update({alias: canonical for alias in spec.aliases})
    return MappingProxyType(aliases)


def canonicalize_fault_name(name: str | None) -> str:
    """Canonicalize one builtin fault name without exposing mutable registry state."""
    raw_name = str(name or "").strip()
    return _builtin_alias_index().get(raw_name, raw_name)


def get_supported_scenario_faults() -> list[str]:
    """Return builtin scenario fault names from an isolated registry."""
    return create_fault_registry().supported_scenario_faults()
