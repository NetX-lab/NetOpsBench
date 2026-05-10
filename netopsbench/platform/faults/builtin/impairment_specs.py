"""Builtin impairment-fault specifications."""

from __future__ import annotations

from ..specs import FaultSpec
from .common import episode_param


def _inject_mtu_mismatch_episode(injector, episode):
    return injector.inject_mtu_mismatch(
        episode.target_device,
        episode.target_interface,
        mtu=episode.mtu or 1400,
    )


def _recover_mtu_mismatch_fault(injector, fault):
    return injector.recover_mtu_mismatch(fault["device"], fault["interface"], fault.get("original_mtu"))


def _inject_packet_corruption_episode(injector, episode):
    return injector.inject_packet_corruption(
        episode.target_device,
        episode.target_interface or "eth1",
        corruption_pct=episode_param(episode, "corruption_pct", 20),
    )


def _inject_packet_loss_episode(injector, episode):
    return injector.inject_packet_loss(
        episode.target_device,
        episode.target_interface or "eth1",
        loss_pct=episode_param(episode, "loss_pct", 10),
    )


def _inject_high_latency_episode(injector, episode):
    return injector.inject_high_latency(
        episode.target_device,
        episode.target_interface or "eth1",
        latency_ms=episode_param(episode, "latency_ms", 100),
    )


def _recover_tc_fault(injector, fault):
    return injector.recover_tc_rules(fault["device"], fault.get("interface", "Ethernet0"))


def build_impairment_fault_specs() -> list[FaultSpec]:
    return [
        FaultSpec(
            name="mtu_mismatch",
            requires_interface=True,
            inject_episode=_inject_mtu_mismatch_episode,
            recover_active_fault=_recover_mtu_mismatch_fault,
        ),
        FaultSpec(
            name="packet_corruption",
            inject_episode=_inject_packet_corruption_episode,
            recover_active_fault=_recover_tc_fault,
        ),
        FaultSpec(
            name="packet_loss",
            inject_episode=_inject_packet_loss_episode,
            recover_active_fault=_recover_tc_fault,
        ),
        FaultSpec(
            name="high_latency",
            inject_episode=_inject_high_latency_episode,
            recover_active_fault=_recover_tc_fault,
        ),
    ]
