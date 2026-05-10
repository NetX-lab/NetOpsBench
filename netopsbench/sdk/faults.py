"""Public fault registry API."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from netopsbench.platform.faults.specs import (
    FaultExecutor,
    FaultPack,
    FaultSpec,
    canonicalize_fault_name,
    get_fault_spec,
    load_builtin_fault_specs,
    register_fault_spec,
)
from netopsbench.sdk.exceptions import (
    FaultNotFoundError,
)
from netopsbench.sdk.types import FaultContext, FaultExecutionResult


class FaultRegistry(Protocol):
    def register(self, *, spec: FaultSpec, executor: FaultExecutor) -> None: ...


@dataclass(frozen=True)
class _FaultRecord:
    spec: FaultSpec
    executor: FaultExecutor


@dataclass(frozen=True)
class _FaultValidationContext:
    target_interface: Any = None
    target_prefix: Any = None
    parameters: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


class _StaticFaultExecutor:
    def __init__(self, spec: FaultSpec):
        self._spec = spec

    def inject(self, context: Any) -> Any:
        if self._spec.inject_episode is None:
            raise NotImplementedError(f"Fault '{self._spec.name}' does not define inject behavior")
        episode = getattr(context, "episode", context)
        return self._spec.inject_episode(context, episode)

    def recover(self, context: Any) -> Any:
        if self._spec.recover_active_fault is None:
            raise NotImplementedError(f"Fault '{self._spec.name}' does not define recover behavior")
        active_fault = getattr(context, "fault", context)
        return self._spec.recover_active_fault(context, active_fault)


class _FunctionFaultExecutor:
    """Wraps plain inject/recover callables into the FaultExecutor protocol.

    Each callable receives a :class:`FaultContext` and may return either a
    :class:`FaultExecutionResult` or a plain ``dict`` — both are accepted.
    """

    def __init__(
        self,
        inject_fn: Callable[[FaultContext], Any],
        recover_fn: Callable[[FaultContext], Any],
    ) -> None:
        self._inject_fn = inject_fn
        self._recover_fn = recover_fn

    @staticmethod
    def _coerce(result: Any, fault_type: str) -> FaultExecutionResult:
        if isinstance(result, FaultExecutionResult):
            return result
        d = dict(result) if result is not None else {}
        return FaultExecutionResult(
            fault_type=fault_type,
            success=bool(d.pop("success", True)),
            error=d.pop("error", None),
            details=d,
        )

    def inject(self, context: FaultContext) -> FaultExecutionResult:
        return self._coerce(self._inject_fn(context), context.fault_type)

    def recover(self, context: FaultContext) -> FaultExecutionResult:
        return self._coerce(self._recover_fn(context), context.fault_type)


class _AnonFaultPack:
    """Minimal FaultPack produced by :func:`simple_fault`."""

    def __init__(self, name: str, spec: FaultSpec, executor: _FunctionFaultExecutor) -> None:
        self.name = name
        self.version: str | None = "1.0"
        self._spec = spec
        self._executor = executor

    def register(self, registry: Any) -> None:
        registry.register(spec=self._spec, executor=self._executor)


def simple_fault(
    name: str,
    *,
    inject: Callable[[FaultContext], Any],
    recover: Callable[[FaultContext], Any],
    requires_interface: bool = False,
    requires_prefix: bool = False,
    required_parameters: tuple[str, ...] = (),
    aliases: list[str] | None = None,
    episode_validator: Callable[[Any], list[str]] | None = None,
    scenario_supported: bool = True,
) -> _AnonFaultPack:
    """Create a fault pack from two plain callables — the simplest way to add a custom fault.

    :param name: Canonical fault name used in scenario YAML ``fault_type`` fields.
    :param inject: ``(ctx: FaultContext) -> dict | FaultExecutionResult`` — inject the fault.
    :param recover: ``(ctx: FaultContext) -> dict | FaultExecutionResult`` — recover the fault.
    :param requires_interface: Set ``True`` when ``target_interface`` is mandatory.
    :param requires_prefix: Set ``True`` when ``target_prefix`` is mandatory.
    :param required_parameters: Tuple of parameter keys that must be present.
    :param aliases: Optional list of alternative names for this fault.
    :param episode_validator: Optional callable for additional episode validation.
    :param scenario_supported: Whether this fault can be used in scenario YAML (default ``True``).
    :returns: A :class:`FaultPack`-compatible object ready for
              ``bench.faults.register_pack()``.

    Example::

        from netopsbench.sdk import simple_fault, FaultContext

        def _inject(ctx: FaultContext) -> dict:
            # ... apply fault ...
            return {"success": True, "device": ctx.target_device}

        def _recover(ctx: FaultContext) -> dict:
            return {"success": True}

        bench.faults.register_pack(
            simple_fault("my_latency", inject=_inject, recover=_recover,
                         requires_interface=True, required_parameters=("delay_ms",))
        )
    """
    spec = FaultSpec(
        name=name,
        requires_interface=requires_interface,
        requires_prefix=requires_prefix,
        required_parameters=tuple(required_parameters),
        aliases=list(aliases or []),
        episode_validator=episode_validator,
        scenario_supported=scenario_supported,
    )
    executor = _FunctionFaultExecutor(inject, recover)
    return _AnonFaultPack(name=name, spec=spec, executor=executor)


class FaultManager:
    """Public in-memory fault registry."""

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace)
        self._entries: dict[str, _FaultRecord] = {}
        self._builtin_loaded = False

    def register(self, *, spec: FaultSpec, executor: FaultExecutor) -> None:
        if not isinstance(spec, FaultSpec):
            raise TypeError("spec must be a FaultSpec")
        if not hasattr(executor, "inject") or not hasattr(executor, "recover"):
            raise TypeError("executor must provide inject() and recover()")

        # Synthesize inject_episode / recover_active_fault wrappers so that
        # scenario_execution.py (which calls spec.inject_episode) can dispatch
        # custom faults through the normal code path.
        _exec = executor

        def _inject_episode(injector: Any, episode: Any) -> dict[str, Any]:
            ctx = FaultContext(
                fault_type=str(getattr(episode, "fault_type", spec.name)),
                target_device=str(getattr(episode, "target_device", "") or ""),
                target_interface=getattr(episode, "target_interface", None),
                parameters=dict(getattr(episode, "parameters", {}) or {}),
                metadata=dict(getattr(episode, "metadata", {}) or {}),
                container_names=dict(getattr(injector, "container_names", {}) or {}),
            )
            outcome = _exec.inject(ctx)
            if isinstance(outcome, FaultExecutionResult):
                return {
                    "type": ctx.fault_type,
                    "device": ctx.target_device,
                    "success": outcome.success,
                    "error": outcome.error,
                    **outcome.details,
                }
            return dict(outcome) if outcome is not None else {"success": True}

        def _recover_active_fault(injector: Any, fault_info: dict[str, Any]) -> dict[str, Any]:
            ctx = FaultContext(
                fault_type=str(fault_info.get("type", spec.name)),
                target_device=str(fault_info.get("device", "") or ""),
                target_interface=fault_info.get("interface"),
                parameters=dict(fault_info.get("parameters", {}) or {}),
                metadata={},
                container_names=dict(getattr(injector, "container_names", {}) or {}),
            )
            outcome = _exec.recover(ctx)
            if isinstance(outcome, FaultExecutionResult):
                return {"success": outcome.success, "error": outcome.error, **outcome.details}
            return dict(outcome) if outcome is not None else {"success": True}

        patched_spec = FaultSpec(
            name=spec.name,
            inject_episode=_inject_episode,
            recover_active_fault=_recover_active_fault,
            requires_interface=spec.requires_interface,
            requires_prefix=spec.requires_prefix,
            required_parameters=tuple(spec.required_parameters),
            episode_validator=spec.episode_validator,
            aliases=list(spec.aliases),
            scenario_supported=spec.scenario_supported,
        )
        register_fault_spec(patched_spec)
        canonical_name = canonicalize_fault_name(spec.name) or spec.name
        registered_spec = get_fault_spec(canonical_name) or patched_spec
        self._entries[canonical_name] = _FaultRecord(spec=registered_spec, executor=executor)

    def register_fault(
        self,
        name: str,
        inject_fn: Callable[[FaultContext], Any],
        recover_fn: Callable[[FaultContext], Any],
        *,
        requires_interface: bool = False,
        requires_prefix: bool = False,
        required_parameters: tuple[str, ...] = (),
        aliases: builtins.list[str] | None = None,
        episode_validator: Callable[[Any], builtins.list[str]] | None = None,
        scenario_supported: bool = True,
    ) -> None:
        """Register a fault from two plain callables — one-liner alternative to register_pack.

        Equivalent to calling :func:`simple_fault` and then :meth:`register_pack`::

            bench.faults.register_fault(
                "my_latency", inject_fn, recover_fn,
                requires_interface=True, required_parameters=("delay_ms",),
            )
        """
        pack = simple_fault(
            name,
            inject=inject_fn,
            recover=recover_fn,
            requires_interface=requires_interface,
            requires_prefix=requires_prefix,
            required_parameters=required_parameters,
            aliases=aliases,
            episode_validator=episode_validator,
            scenario_supported=scenario_supported,
        )
        self.register_pack(pack)

    def register_pack(self, pack: FaultPack) -> None:
        pack_name = str(getattr(pack, "name", "")).strip()
        if not pack_name:
            raise ValueError("pack.name must be a non-empty string")
        pack.register(self)

    def load_builtin(self) -> None:
        if self._builtin_loaded:
            return
        for spec in load_builtin_fault_specs():
            canonical_name = canonicalize_fault_name(spec.name) or spec.name
            if canonical_name in self._entries:
                continue
            self._entries[canonical_name] = _FaultRecord(spec=spec, executor=_StaticFaultExecutor(spec))
        self._builtin_loaded = True

    def load_plugin(self, plugin_ref: str) -> None:
        module_path, separator, attr_name = str(plugin_ref or "").strip().partition(":")
        if not separator or not module_path or not attr_name:
            raise ValueError("plugin_ref must use the form 'module_path:FaultPackClass' or 'module_path:factory'")
        module = import_module(module_path)
        plugin = getattr(module, attr_name)
        if isinstance(plugin, type):
            plugin = plugin()
        if not hasattr(plugin, "register"):
            raise TypeError("plugin_ref must resolve to a FaultPack-like object with register()")
        self.register_pack(plugin)

    def list(self) -> builtins.list[FaultSpec]:
        return [record.spec for _, record in sorted(self._entries.items(), key=lambda item: item[0])]

    def get(self, name: str) -> FaultSpec:
        canonical_name = canonicalize_fault_name(name)
        if not canonical_name:
            raise FaultNotFoundError("fault name must be a non-empty string")
        record = self._entries.get(canonical_name)
        if record is None:
            raise FaultNotFoundError(f"Fault '{canonical_name}' not found")
        return record.spec

    def get_executor(self, name: str) -> FaultExecutor:
        canonical_name = canonicalize_fault_name(name)
        if not canonical_name:
            raise FaultNotFoundError("fault name must be a non-empty string")
        record = self._entries.get(canonical_name)
        if record is None:
            raise FaultNotFoundError(f"Fault '{canonical_name}' not found")
        return record.executor

    def validate_parameters(self, fault_type: str, parameters: dict[str, object]) -> builtins.list[str]:
        canonical_name = canonicalize_fault_name(fault_type)
        if not canonical_name:
            return ["Unsupported fault type: "]
        record = self._entries.get(canonical_name)
        if record is None:
            return [f"Unsupported fault type: {canonical_name}"]
        proxy = _FaultValidationContext(
            target_interface=parameters.get("target_interface"),
            target_prefix=parameters.get("target_prefix"),
            parameters=dict(parameters or {}),
            metadata={},
        )
        return record.spec.validate_episode(proxy)


__all__ = ["FaultRegistry", "FaultManager", "simple_fault"]
