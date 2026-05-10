"""Builtin system-fault specifications."""

from __future__ import annotations

from ..specs import FaultSpec


def _inject_device_down_episode(injector, episode):
    return injector.inject_device_down(episode.target_device)


def _recover_device_down_fault(injector, fault):
    return injector.recover_device_down(fault["device"], fault.get("interfaces"))


def build_system_fault_specs() -> list[FaultSpec]:
    return [
        FaultSpec(
            name="device_down",
            inject_episode=_inject_device_down_episode,
            recover_active_fault=_recover_device_down_fault,
        ),
    ]
