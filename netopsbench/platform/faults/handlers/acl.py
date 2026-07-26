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
    from ..context import FaultRuntimeContext
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
        ctx: FaultRuntimeContext,
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
    def _iptables_rule_args(target_prefix: str, acl_name: str) -> list[str]:
        return [
            "FORWARD",
            "-d",
            target_prefix,
            "-j",
            "DROP",
            "-m",
            "comment",
            "--comment",
            f"{_IPTABLES_TAG}:{acl_name}",
        ]

    def _acl_state_present(self, container: str, target_prefix: str, acl_name: str) -> bool:
        rule = self._cmd.docker_exec(
            container,
            ["iptables", "-C", *self._iptables_rule_args(target_prefix, acl_name)],
        )
        table = self._cmd.docker_exec(
            container,
            ["sonic-db-cli", "CONFIG_DB", "exists", f"ACL_TABLE|{acl_name}"],
        )
        acl_rule = self._cmd.docker_exec(
            container,
            ["sonic-db-cli", "CONFIG_DB", "exists", f"ACL_RULE|{acl_name}|RULE_1"],
        )
        return rule.returncode == 0 and (table.stdout or "").strip() == "1" and (acl_rule.stdout or "").strip() == "1"

    def _acl_state_absent(self, container: str, target_prefix: str, acl_name: str) -> bool:
        rule = self._cmd.docker_exec(
            container,
            ["iptables", "-C", *self._iptables_rule_args(target_prefix, acl_name)],
        )
        table = self._cmd.docker_exec(
            container,
            ["sonic-db-cli", "CONFIG_DB", "exists", f"ACL_TABLE|{acl_name}"],
        )
        acl_rule = self._cmd.docker_exec(
            container,
            ["sonic-db-cli", "CONFIG_DB", "exists", f"ACL_RULE|{acl_name}|RULE_1"],
        )
        return rule.returncode == 1 and (table.stdout or "").strip() == "0" and (acl_rule.stdout or "").strip() == "0"

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
            for link in self._ctx.manifest.links:
                for endpoint in link.endpoints:
                    if endpoint.device == device:
                        interface = endpoint.interface
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
                *self._iptables_rule_args(target_prefix, acl_name),
            ],
        )

        # --- 2. CONFIG_DB breadcrumb: visible via 'show acl table/rule' ---
        if iptables_result.returncode == 0:
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

        state_present = self._acl_state_present(container, target_prefix, acl_name)
        fault_info: dict[str, Any] = {
            "type": "acl_misconfig",
            "device": device,
            "target_prefix": target_prefix,
            "interface": interface,
            "direction": direction,
            "acl_name": acl_name,
            "success": state_present,
            "error": (
                None
                if state_present
                else (iptables_result.stderr or iptables_result.stdout or "").strip()
                or "ACL data-plane and CONFIG_DB state did not agree"
            ),
        }

        if state_present:
            self._tracker.track(fault_info)
            return fault_info

        rollback = self.recover_acl_misconfig(
            device,
            target_prefix,
            interface=interface,
            direction=direction,
            acl_name=acl_name,
        )
        if not rollback["recovered"]:
            error = "; ".join(
                filter(
                    None,
                    [
                        str(fault_info.get("error") or ""),
                        str(rollback.get("error") or "ACL compensation failed"),
                    ],
                )
            )
            fault_info["error"] = error
            self._tracker.track_residual(fault_info, error)
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
                *self._iptables_rule_args(target_prefix, acl_name),
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

        recovered = self._acl_state_absent(container, target_prefix, acl_name)
        if recovered:
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
            "recovered": recovered,
            "error": None if recovered else iptables_result.stderr or "ACL state remained after recovery",
        }
