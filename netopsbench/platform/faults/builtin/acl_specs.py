"""Builtin ACL-fault specifications."""

from __future__ import annotations

from ..specs import FaultSpec
from .common import episode_param


def _inject_acl_misconfig_episode(injector, episode):
    return injector.inject_acl_misconfig(
        episode.target_device,
        episode.target_prefix or episode_param(episode, "target_prefix"),
        episode_param(episode, "interface") or getattr(episode, "target_interface", None),
        episode_param(episode, "direction", "in"),
    )


def _recover_acl_misconfig_fault(injector, fault):
    return injector.recover_acl_misconfig(
        fault["device"],
        fault["target_prefix"],
        fault.get("interface"),
        fault.get("direction", "in"),
        fault.get("acl_name"),
    )


def build_acl_fault_specs() -> list[FaultSpec]:
    return [
        FaultSpec(
            name="acl_misconfig",
            aliases=["acl_misconfiguration"],
            inject_episode=_inject_acl_misconfig_episode,
            recover_active_fault=_recover_acl_misconfig_fault,
        ),
    ]
