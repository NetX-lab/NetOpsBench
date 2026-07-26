"""Static-route and blackhole routing fault handlers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class StaticRouteHandler:
    """Handles static-route and blackhole routing fault injection and recovery."""

    def __init__(
        self,
        sonic: SonicRuntime,
        tracker: FaultTracker,
        ctx: FaultRuntimeContext,
    ) -> None:
        self._sonic = sonic
        self._tracker = tracker
        self._ctx = ctx

    def _running_config_contains(self, device: str, statement: str) -> bool | None:
        result = self._sonic.vtysh(device, ["show running-config"])
        if result.returncode != 0:
            return None
        expected = " ".join(statement.split())
        return any(" ".join(line.strip().split()) == expected for line in (result.stdout or "").splitlines())

    def _running_config_has_route(self, device: str, target: str) -> bool | None:
        result = self._sonic.vtysh(device, ["show running-config"])
        if result.returncode != 0:
            return None
        prefix = f"ip route {target} "
        return any(" ".join(line.strip().split()).startswith(prefix) for line in (result.stdout or "").splitlines())

    def _operational_route(self, device: str, target: str) -> str | None:
        result = self._sonic.vtysh(device, [f"show ip route {target}"])
        if result.returncode != 0:
            return None
        return result.stdout or ""

    def _operational_blackhole_present(self, device: str, target: str) -> bool | None:
        output = self._operational_route(device, target)
        if output is None:
            return None
        return bool(target in output and re.search(r"\b(?:blackhole|discard|Null0)\b", output, re.IGNORECASE))

    def _operational_nexthop_present(
        self,
        device: str,
        target: str,
        nexthop: str,
    ) -> bool | None:
        output = self._operational_route(device, target)
        if output is None:
            return None
        return bool(target in output and re.search(rf"\b(?:via\s+)?{re.escape(nexthop)}\b", output))

    def _track_failed_compensation(
        self,
        fault_info: dict[str, Any],
        rollback: dict[str, Any],
    ) -> None:
        if rollback.get("recovered") is True:
            return
        error = "; ".join(
            filter(
                None,
                [
                    str(fault_info.get("error") or ""),
                    str(rollback.get("error") or "route compensation failed"),
                ],
            )
        )
        fault_info["error"] = error
        self._tracker.track_residual(fault_info, error)

    # ------------------------------------------------------------------
    # Topology helpers (moved from FaultInjector body)
    # ------------------------------------------------------------------

    def _pick_reachable_wrong_nexthop(self, target_device: str, target_ip: str) -> str | None:
        """Pick a reachable but incorrect next-hop IP for static route misconfig."""
        if not self._ctx.clients:
            return None

        target_ip_str = (target_ip or "").split("/")[0]
        candidates = [c for c in self._ctx.clients if c.get("data_ip") and c.get("data_ip") != target_ip_str]
        if not candidates:
            return None

        local = [c for c in self._ctx.clients_by_leaf.get(target_device, []) if c.get("data_ip") != target_ip_str]
        pool = local if local else candidates
        pool_sorted = sorted(pool, key=lambda c: c.get("name", ""))
        return pool_sorted[0].get("data_ip")

    def _pick_remote_client_host_route(self, target_device: str) -> str | None:
        """Pick a remote client /32 so route faults affect fabric traffic across scales."""
        candidates = [c for c in self._ctx.clients if c.get("data_ip")]
        if not candidates:
            return None

        remote = [c for c in candidates if c.get("attached_switch") != target_device]
        if remote:
            candidates = remote

        chosen = sorted(candidates, key=lambda c: (c.get("attached_switch", ""), c.get("name", "")))[0]
        ip_str = str(chosen.get("data_ip") or "").split("/")[0].strip()
        if not ip_str:
            return None
        return f"{ip_str}/32"

    def _resolve_static_route_target_ip(self, target_device: str, target_ip: str | None) -> str | None:
        """Resolve static-route targets dynamically when configs omit a topology-specific host /32."""
        raw = str(target_ip or "").strip()
        if not raw or raw.lower() == "auto":
            return self._pick_remote_client_host_route(target_device)
        if "/" not in raw:
            return f"{raw}/32"
        return raw

    # ------------------------------------------------------------------
    # Blackhole route
    # ------------------------------------------------------------------

    def inject_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        """Inject blackhole route to silently drop traffic to a prefix."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        result = self._sonic.vtysh(
            device,
            [
                "configure terminal",
                f"ip route {target_prefix} Null0",
                "end",
                "write memory",
            ],
        )

        statement = f"ip route {target_prefix} Null0"
        success = (
            result.returncode == 0
            and self._running_config_contains(device, statement) is True
            and self._operational_blackhole_present(device, target_prefix) is True
        )
        fault_info = {
            "type": "blackhole_route",
            "device": device,
            "prefix": target_prefix,
            "success": success,
            "error": None if success else result.stderr or f"blackhole route was not operational: {statement}",
        }

        if success:
            self._tracker.track(fault_info)
            return fault_info

        rollback = self.recover_blackhole_route(device, target_prefix)
        self._track_failed_compensation(fault_info, rollback)
        return fault_info

    def recover_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        """Remove blackhole route."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        result = self._sonic.vtysh(
            device,
            [
                "configure terminal",
                f"no ip route {target_prefix} Null0",
                "end",
                "write memory",
            ],
        )

        removed = (
            result.returncode == 0
            and self._running_config_contains(device, f"ip route {target_prefix} Null0") is False
            and self._operational_blackhole_present(device, target_prefix) is False
        )
        if removed:
            self._tracker.remove_faults(
                lambda fault: fault["type"] == "blackhole_route"
                and fault["device"] == device
                and fault["prefix"] == target_prefix
            )

        return {
            "type": "blackhole_route",
            "device": device,
            "prefix": target_prefix,
            "recovered": removed,
            "error": None if removed else result.stderr or "blackhole route remained in running config",
        }

    # ------------------------------------------------------------------
    # Static route misconfig
    # ------------------------------------------------------------------

    def inject_static_route_misconfig(
        self, device: str, target_ip: str | None = None, wrong_nexthop: str | None = None
    ) -> dict[str, Any]:
        """Inject static route misconfiguration pointing to wrong next-hop."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        target_ip = self._resolve_static_route_target_ip(device, target_ip)
        if not target_ip:
            raise RuntimeError(f"Unable to determine target host route from topology metadata: device={device}")

        if not wrong_nexthop or str(wrong_nexthop).lower() == "auto":
            wrong_nexthop = self._pick_reachable_wrong_nexthop(device, target_ip)
            if not wrong_nexthop:
                raise RuntimeError(
                    f"Unable to determine reachable wrong next-hop from topology metadata: device={device} target_ip={target_ip}"
                )

        result = self._sonic.vtysh(
            device,
            [
                "configure terminal",
                f"ip route {target_ip} {wrong_nexthop}",
                "end",
                "write memory",
            ],
        )

        statement = f"ip route {target_ip} {wrong_nexthop}"
        success = (
            result.returncode == 0
            and self._running_config_contains(device, statement) is True
            and self._operational_nexthop_present(device, target_ip, wrong_nexthop) is True
        )
        fault_info = {
            "type": "static_route_misconfig",
            "device": device,
            "target_ip": target_ip,
            "wrong_nexthop": wrong_nexthop,
            "success": success,
            "error": None if success else result.stderr or f"static route was not operational: {statement}",
        }

        if success:
            self._tracker.track(fault_info)
            return fault_info

        rollback = self.recover_static_route_misconfig(device, target_ip, wrong_nexthop)
        self._track_failed_compensation(fault_info, rollback)
        return fault_info

    def recover_static_route_misconfig(
        self,
        device: str,
        target_ip: str,
        wrong_nexthop: str | None = None,
    ) -> dict[str, Any]:
        """Remove misconfigured static route."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        command_sets = []
        if wrong_nexthop:
            command_sets.append(
                [
                    "configure terminal",
                    f"no ip route {target_ip} {wrong_nexthop}",
                    "end",
                    "write memory",
                ]
            )
        command_sets.append(
            [
                "configure terminal",
                f"no ip route {target_ip}",
                "end",
                "write memory",
            ]
        )

        result = None
        for commands in command_sets:
            result = self._sonic.vtysh(device, commands)
            if result.returncode == 0:
                break
        assert result is not None

        removed = (
            result.returncode == 0
            and self._running_config_has_route(device, target_ip) is False
            and (not wrong_nexthop or self._operational_nexthop_present(device, target_ip, wrong_nexthop) is False)
        )
        if removed:
            self._tracker.remove_faults(
                lambda fault: fault["type"] == "static_route_misconfig"
                and fault["device"] == device
                and fault["target_ip"] == target_ip
            )

        return {
            "type": "static_route_misconfig",
            "device": device,
            "target_ip": target_ip,
            "wrong_nexthop": wrong_nexthop,
            "recovered": removed,
            "error": None if removed else result.stderr or "static route remained in running config",
        }
