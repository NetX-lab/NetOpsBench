"""Static-route and blackhole routing fault handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class StaticRouteHandler:
    """Handles static-route and blackhole routing fault injection and recovery."""

    def __init__(
        self,
        sonic: SonicRuntime,
        tracker: FaultTracker,
        ctx: FaultContext,
    ) -> None:
        self._sonic = sonic
        self._tracker = tracker
        self._ctx = ctx

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

        remote = [c for c in candidates if c.get("leaf") != target_device]
        if remote:
            candidates = remote

        chosen = sorted(candidates, key=lambda c: (c.get("leaf", ""), c.get("name", "")))[0]
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

        fault_info = {
            "type": "blackhole_route",
            "device": device,
            "prefix": target_prefix,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }

        if fault_info["success"]:
            self._tracker.track(fault_info)

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

        self._tracker.remove_faults(
            lambda fault: fault["type"] == "blackhole_route"
            and fault["device"] == device
            and fault["prefix"] == target_prefix
        )

        return {
            "type": "blackhole_route",
            "device": device,
            "prefix": target_prefix,
            "recovered": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
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

        fault_info = {
            "type": "static_route_misconfig",
            "device": device,
            "target_ip": target_ip,
            "wrong_nexthop": wrong_nexthop,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }

        if fault_info["success"]:
            self._tracker.track(fault_info)

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
            "recovered": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
