"""Centralized fault extension metadata and dispatch registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

_CANONICAL_FAULT_ALIASES: dict[str, str] = {
    "static_route_misconfiguration": "static_route_misconfig",
}


@dataclass
class FaultSpec:
    name: str
    inject_episode: Callable[[Any, Any], dict[str, Any]] | None = None
    recover_active_fault: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    requires_interface: bool = False
    requires_prefix: bool = False
    required_parameters: tuple[str, ...] = ()
    episode_validator: Callable[[Any], list[str]] | None = None
    aliases: list[str] = field(default_factory=list)
    scenario_supported: bool = True

    def validate_episode(self, episode: Any, episode_index: int | None = None) -> list[str]:
        """Validate an episode against this fault spec."""
        errors: list[str] = []
        prefix = f"Episode {episode_index}: " if episode_index is not None else ""

        if self.requires_interface and not getattr(episode, "target_interface", None):
            errors.append(f"{prefix}{self.name} requires target_interface")

        if self.requires_prefix and not getattr(episode, "target_prefix", None):
            errors.append(f"{prefix}{self.name} requires target_prefix")

        if self.required_parameters:
            parameters = getattr(episode, "parameters", {}) or {}
            metadata = getattr(episode, "metadata", {}) or {}
            for key in self.required_parameters:
                value = parameters.get(key, metadata.get(key))
                if value in (None, "", [], {}, ()):
                    errors.append(f"{prefix}{self.name} requires parameter '{key}'")

        if self.episode_validator is not None:
            errors.extend(self.episode_validator(episode))

        return errors


class FaultExecutor(Protocol):
    """Public protocol for fault injection/recovery executors."""

    def inject(self, context: Any) -> Any:
        """Inject a fault for the provided context."""

    def recover(self, context: Any) -> Any:
        """Recover a fault for the provided context."""


class FaultPack(Protocol):
    """Public protocol for grouping related fault registrations."""

    name: str
    version: str | None

    def register(self, registry: Any) -> None:
        """Register faults into the provided registry."""


# ---------------------------------------------------------------------------
# FaultSpecRegistry — encapsulates all mutable spec state
# ---------------------------------------------------------------------------


class FaultSpecRegistry:
    """Registry holding fault specs, aliases, and schema defaults.

    A default module-level instance powers the convenience functions below.
    Tests can create isolated instances to avoid cross-test pollution.
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
        for key, value in defaults.items():
            if getattr(spec, key) in (False, None, (), []):
                setattr(spec, key, value)
        normalized_aliases = sorted(
            {
                alias
                for alias in ([spec.name] + list(spec.aliases) + [canonical_name])
                if alias and alias != canonical_name
            }
        )
        spec.name = canonical_name
        spec.aliases = normalized_aliases
        self._specs[canonical_name] = spec
        self._aliases[canonical_name] = canonical_name
        for alias in normalized_aliases:
            self._aliases[alias] = canonical_name
        return spec

    def unregister(self, name: str) -> None:
        canonical_name = self.canonicalize(name)
        spec = self._specs.pop(canonical_name, None)
        if spec is None:
            return
        self._aliases.pop(canonical_name, None)
        for alias in spec.aliases:
            self._aliases.pop(alias, None)
        for alias, target in list(_CANONICAL_FAULT_ALIASES.items()):
            if target == canonical_name:
                self._aliases[alias] = target

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
# Default module-level instance + backward-compatible convenience functions
# ---------------------------------------------------------------------------

_default_registry = FaultSpecRegistry()


def canonicalize_fault_name(name: str | None) -> str:
    return _default_registry.canonicalize(name)


def register_fault_spec(spec: FaultSpec) -> FaultSpec:
    return _default_registry.register(spec)


def unregister_fault_spec(name: str) -> None:
    _default_registry.unregister(name)


def get_fault_spec(name: str) -> FaultSpec | None:
    return _default_registry.get(name)


def list_fault_specs() -> list[FaultSpec]:
    return _default_registry.list_all()


def get_supported_scenario_faults() -> list[str]:
    return _default_registry.supported_scenario_faults()


def get_builtin_fault_specs() -> list[FaultSpec]:
    """Return the builtin fault specs that ship with NetOpsBench."""
    return _default_registry.get_builtin_specs()


def load_builtin_fault_specs() -> list[Any]:
    """Ensure builtin fault specs are registered and return a snapshot."""
    return list(get_builtin_fault_specs())


# ---------------------------------------------------------------------------
# Builtin spec aggregation (formerly netopsbench.platform.faults.builtin_specs)
# ---------------------------------------------------------------------------


def _build_builtin_fault_specs() -> list[FaultSpec]:
    """Aggregate every builtin spec builder into a single list.

    Imported lazily inside the function to avoid an import cycle:
    ``builtin/__init__`` may transitively import ``specs`` for shared types.
    """
    from .builtin import (
        build_acl_fault_specs,
        build_impairment_fault_specs,
        build_link_fault_specs,
        build_routing_fault_specs,
        build_system_fault_specs,
    )

    builders: tuple[Callable[[], list[FaultSpec]], ...] = (
        build_link_fault_specs,
        build_routing_fault_specs,
        build_impairment_fault_specs,
        build_system_fault_specs,
        build_acl_fault_specs,
    )
    aggregated: list[FaultSpec] = []
    for build_specs in builders:
        aggregated.extend(build_specs())
    return aggregated


def register_builtin_fault_specs() -> None:
    """Register every builtin fault spec into the default registry.

    Equivalent to :func:`load_builtin_fault_specs` but does not return the
    snapshot. Kept for backward compatibility with external callers that
    imported the previous ``builtin_specs`` module.
    """
    for spec in _build_builtin_fault_specs():
        register_fault_spec(spec)
