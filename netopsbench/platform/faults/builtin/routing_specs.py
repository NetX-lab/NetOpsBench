"""Builtin routing-fault specifications."""

from __future__ import annotations

from ..models import FaultSpec
from .common import episode_param


def _inject_blackhole_route_episode(injector, episode):
    return injector.inject_blackhole_route(
        episode.target_device,
        episode.target_prefix or "192.168.0.0/16",
    )


def _recover_blackhole_route_fault(injector, fault):
    return injector.recover_blackhole_route(fault["device"], fault["prefix"])


def _inject_static_route_misconfig_episode(injector, episode):
    return injector.inject_static_route_misconfig(
        episode.target_device,
        episode_param(episode, "target_ip"),
        episode_param(episode, "wrong_nexthop"),
    )


def _recover_static_route_misconfig_fault(injector, fault):
    return injector.recover_static_route_misconfig(
        fault["device"],
        fault["target_ip"],
        fault.get("wrong_nexthop"),
    )


def build_routing_fault_specs() -> list[FaultSpec]:
    return [
        FaultSpec(
            name="blackhole_route",
            inject_episode=_inject_blackhole_route_episode,
            recover_active_fault=_recover_blackhole_route_fault,
        ),
        FaultSpec(
            name="static_route_misconfig",
            aliases=["static_route_misconfiguration"],
            inject_episode=_inject_static_route_misconfig_episode,
            recover_active_fault=_recover_static_route_misconfig_fault,
        ),
    ]
