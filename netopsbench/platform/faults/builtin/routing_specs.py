"""Builtin routing-fault specifications."""

from __future__ import annotations

from ..specs import FaultSpec
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


def _inject_bgp_neighbor_misconfig_episode(injector, episode):
    return injector.inject_bgp_neighbor_misconfig(
        episode.target_device,
        peer_ip=episode_param(episode, "peer_ip"),
        misconfig_kind=episode_param(episode, "misconfig_kind", "peer_as_mismatch"),
        wrong_remote_as=episode_param(episode, "wrong_remote_as"),
        password=episode_param(episode, "password"),
        update_source=episode_param(episode, "update_source"),
    )


def _recover_bgp_neighbor_misconfig_fault(injector, fault):
    return injector.recover_bgp_neighbor_misconfig(
        fault["device"],
        fault["peer_ip"],
        fault.get("misconfig_kind", "peer_as_mismatch"),
        original_remote_as=fault.get("original_remote_as"),
        original_password=fault.get("original_password"),
        original_update_source=fault.get("original_update_source"),
        wrong_remote_as=fault.get("wrong_remote_as"),
        bad_update_source=fault.get("bad_update_source"),
    )


def _inject_route_policy_misconfig_episode(injector, episode):
    return injector.inject_route_policy_misconfig(
        episode.target_device,
        target_prefix=episode.target_prefix or episode_param(episode, "target_prefix"),
        misconfig_kind=episode_param(episode, "misconfig_kind", "network_statement_missing"),
        route_map=episode_param(episode, "route_map"),
    )


def _recover_route_policy_misconfig_fault(injector, fault):
    return injector.recover_route_policy_misconfig(
        fault["device"],
        fault["target_prefix"],
        fault.get("misconfig_kind", "network_statement_missing"),
        route_map=fault.get("route_map"),
        network_statement=fault.get("network_statement"),
        prefix_list_name=fault.get("prefix_list_name"),
        sequence=fault.get("sequence"),
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
        FaultSpec(
            name="bgp_neighbor_misconfig",
            inject_episode=_inject_bgp_neighbor_misconfig_episode,
            recover_active_fault=_recover_bgp_neighbor_misconfig_fault,
        ),
        FaultSpec(
            name="route_policy_misconfig",
            inject_episode=_inject_route_policy_misconfig_episode,
            recover_active_fault=_recover_route_policy_misconfig_fault,
        ),
    ]
