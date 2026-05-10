"""ACL misconfiguration fault handlers.

Uses iptables for actual data-plane filtering (SONiC-VS forwards via Linux
kernel, so iptables rules are effective).  A parallel SONiC CONFIG_DB
``ACL_TABLE`` / ``ACL_RULE`` entry is also written so that standard SONiC
diagnostic commands (``show acl table``, ``show acl rule``) reveal the
misconfiguration to the diagnosing agent — matching the real-device workflow.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.command_runner import CommandRunner
    from ..services.routing_runtime import RoutingRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker

# iptables comment used to tag injected rules so we can selectively remove them.
_IPTABLES_TAG = "NETOPSBENCH_ACL"

# Regex to validate prefix format (basic check)
_PREFIX_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")


class AclHandler:
    """Handles ACL misconfiguration fault injection and recovery.

    Injects both:
    * **iptables DROP** rule on the SONiC container – this is the rule that
      actually blocks forwarded traffic on the Linux data plane.
    * **CONFIG_DB ACL_TABLE / ACL_RULE** – a SONiC-native breadcrumb visible
      via ``show acl table`` and ``show acl rule`` so the agent can discover
      the misconfiguration using standard SONiC diagnostic commands.
    """

    _ACL_NAME_PREFIX = "NETOPSBENCH_DENY"

    def __init__(
        self,
        cmd: CommandRunner,
        sonic: SonicRuntime,
        routing: RoutingRuntime,
        tracker: FaultTracker,
        ctx: FaultContext,
    ) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._routing = routing
        self._tracker = tracker
        self._ctx = ctx

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _acl_name(self, device: str, prefix: str) -> str:
        safe = prefix.replace("/", "_").replace(".", "-")
        return f"{self._ACL_NAME_PREFIX}_{device}_{safe}"

    @staticmethod
    def _validate_prefix(prefix: str) -> str:
        if not _PREFIX_RE.match(prefix):
            raise ValueError(f"Invalid prefix format: {prefix}")
        return prefix

    # ------------------------------------------------------------------
    # inject
    # ------------------------------------------------------------------

    def inject_acl_misconfig(
        self,
        device: str,
        target_prefix: str | None = None,
        interface: str | None = None,
        direction: str = "in",
    ) -> dict[str, Any]:
        """Inject an ACL that denies traffic matching *target_prefix*.

        1. Adds an **iptables FORWARD DROP** rule in the SONiC container so
           that forwarded packets to/from *target_prefix* are actually dropped
           on the Linux data plane.
        2. Writes matching **CONFIG_DB ACL_TABLE / ACL_RULE** entries so that
           ``show acl table`` and ``show acl rule`` reveal the misconfiguration
           to the diagnosing agent.
        """
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        # Resolve the prefix to deny
        if not target_prefix:
            network = self._routing.pick_advertised_network(device)
            if network:
                target_prefix = str(network["prefix"])
            else:
                target_prefix = "10.0.0.0/8"

        self._validate_prefix(target_prefix)

        # Resolve interface (for vtysh breadcrumb; iptables rule is global FORWARD)
        if not interface:
            topo = self._ctx.topology_metadata
            links = topo.get("links", [])
            for link in links:
                endpoints = link.get("endpoints", [])
                interfaces = link.get("interfaces", [])
                for idx, ep in enumerate(endpoints):
                    # endpoints can be strings ("spine1") or dicts ({"device": "spine1", "interface": "Ethernet0"})
                    ep_device = ep.get("device") if isinstance(ep, dict) else ep
                    if ep_device == device:
                        if isinstance(ep, dict) and ep.get("interface"):
                            interface = ep["interface"]
                        elif interfaces and idx < len(interfaces):
                            interface = interfaces[idx]
                        break
                if interface:
                    break
            # Fallback: query the device for the first Ethernet interface
            if not interface:
                try:
                    container_name = self._ctx.container_names.get(device)
                    if container_name:
                        result = self._cmd.docker_exec(
                            container_name,
                            [
                                "vtysh",
                                "-c",
                                "show interface brief",
                            ],
                        )
                        if result.returncode == 0:
                            for line in result.stdout.splitlines():
                                parts = line.split()
                                if parts and parts[0].startswith("Ethernet"):
                                    interface = parts[0]
                                    break
                except Exception:
                    logger.debug("failed to detect interface on %s for ACL binding", device, exc_info=True)
                    pass
            if not interface:
                raise RuntimeError(f"Unable to determine interface for ACL binding: device={device}")

        direction = direction.lower()
        if direction not in ("in", "out"):
            direction = "in"

        acl_name = self._acl_name(device, target_prefix)

        # --- 1. iptables: actually block forwarded traffic ----------------
        iptables_result = self._cmd.docker_exec(
            container,
            [
                "iptables",
                "-I",
                "FORWARD",
                "-d",
                target_prefix,
                "-j",
                "DROP",
                "-m",
                "comment",
                "--comment",
                f"{_IPTABLES_TAG}:{acl_name}",
            ],
        )

        if iptables_result.returncode != 0:
            raise RuntimeError(f"iptables failed on {device}: {iptables_result.stderr}")

        # --- 2. CONFIG_DB breadcrumb: visible via 'show acl table/rule' ---
        stage = "ingress" if direction == "in" else "egress"
        self._cmd.docker_exec(
            container,
            [
                "sonic-db-cli",
                "CONFIG_DB",
                "hset",
                f"ACL_TABLE|{acl_name}",
                "policy_desc",
                f"netopsbench injected deny {target_prefix}",
                "type",
                "L3",
                "stage",
                stage,
                "ports@",
                interface,
            ],
        )
        self._cmd.docker_exec(
            container,
            [
                "sonic-db-cli",
                "CONFIG_DB",
                "hset",
                f"ACL_RULE|{acl_name}|RULE_1",
                "PRIORITY",
                "999",
                "PACKET_ACTION",
                "DROP",
                "DST_IP",
                target_prefix,
            ],
        )

        fault_info: dict[str, Any] = {
            "type": "acl_misconfig",
            "device": device,
            "target_prefix": target_prefix,
            "interface": interface,
            "direction": direction,
            "acl_name": acl_name,
            "success": True,
            "error": None,
        }

        self._tracker.track(fault_info)
        return fault_info

    # ------------------------------------------------------------------
    # recover
    # ------------------------------------------------------------------

    def recover_acl_misconfig(
        self,
        device: str,
        target_prefix: str,
        interface: str | None = None,
        direction: str = "in",
        acl_name: str | None = None,
    ) -> dict[str, Any]:
        """Remove the previously injected ACL deny rule."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        acl_name = acl_name or self._acl_name(device, target_prefix)
        direction = (direction or "in").lower()

        # --- 1. Remove iptables rule --------------------------------------
        iptables_result = self._cmd.docker_exec(
            container,
            [
                "iptables",
                "-D",
                "FORWARD",
                "-d",
                target_prefix,
                "-j",
                "DROP",
                "-m",
                "comment",
                "--comment",
                f"{_IPTABLES_TAG}:{acl_name}",
            ],
        )

        # --- 2. Remove CONFIG_DB breadcrumb --------------------------------
        self._cmd.docker_exec(
            container,
            [
                "sonic-db-cli",
                "CONFIG_DB",
                "del",
                f"ACL_RULE|{acl_name}|RULE_1",
            ],
        )
        self._cmd.docker_exec(
            container,
            [
                "sonic-db-cli",
                "CONFIG_DB",
                "del",
                f"ACL_TABLE|{acl_name}",
            ],
        )

        self._tracker.remove_faults(
            lambda fault: fault["type"] == "acl_misconfig"
            and fault["device"] == device
            and fault.get("target_prefix") == target_prefix
        )

        return {
            "type": "acl_misconfig",
            "device": device,
            "target_prefix": target_prefix,
            "interface": interface,
            "acl_name": acl_name,
            "recovered": iptables_result.returncode == 0,
            "error": iptables_result.stderr if iptables_result.returncode != 0 else None,
        }
