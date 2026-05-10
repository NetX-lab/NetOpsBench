"""Builtin link-fault specifications."""

from __future__ import annotations

from ..specs import FaultSpec
from .common import episode_param, recover_background_process_fault


def _inject_link_down_episode(injector, episode):
    return injector.inject_link_down(episode.target_device, episode.target_interface)


def _recover_link_down_fault(injector, fault):
    return injector.recover_link_down(fault["device"], fault["interface"])


def _inject_link_flapping_episode(injector, episode):
    return injector.inject_link_flapping(
        device=episode.target_device,
        interface=episode.target_interface or "Ethernet0",
        iterations=episode_param(episode, "iterations", 10),
        down_time=episode_param(episode, "down_time", 2),
        up_time=episode_param(episode, "up_time", 3),
    )


def build_link_fault_specs() -> list[FaultSpec]:
    return [
        FaultSpec(
            name="link_down",
            requires_interface=True,
            inject_episode=_inject_link_down_episode,
            recover_active_fault=_recover_link_down_fault,
        ),
        FaultSpec(
            name="link_flapping",
            inject_episode=_inject_link_flapping_episode,
            recover_active_fault=recover_background_process_fault,
        ),
    ]
