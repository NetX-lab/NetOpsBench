"""
Fault Injector - Programmatic interface for injecting network faults.

Supports various fault types for DCN troubleshooting benchmark.
"""

import json
import os
from typing import Any

from netopsbench.config import config
from netopsbench.platform.faults.context import FaultContext
from netopsbench.platform.faults.handlers.acl import AclHandler
from netopsbench.platform.faults.handlers.impairment import ImpairmentHandler
from netopsbench.platform.faults.handlers.link import LinkHandler
from netopsbench.platform.faults.handlers.routing_bgp import BgpHandler
from netopsbench.platform.faults.handlers.routing_policy import RoutePolicyHandler
from netopsbench.platform.faults.handlers.routing_static import StaticRouteHandler
from netopsbench.platform.faults.handlers.system import SystemHandler
from netopsbench.platform.faults.services import (
    CommandRunner,
    FaultTracker,
    InterfaceRuntime,
    RoutingRuntime,
    SonicRuntime,
    TopologyRuntime,
)
from netopsbench.platform.faults.specs import get_fault_spec
from netopsbench.platform.topology.topology_utils import (
    build_topology_state_from_metadata,
    discover_topology_dir,
)
from netopsbench.platform.utils.result import OperationResult


class FaultInjector:
    """
    Manages fault injection for network troubleshooting benchmarks.

    Provides methods to inject and recover from various network faults.
    Uses composition: each concern lives in a dedicated service or handler class.
    """

    SONIC_MIN_INTERFACE_MTU = 68
    SONIC_MAX_INTERFACE_MTU = 9216
    SONIC_DEFAULT_INTERFACE_MTU = 9100

    def __init__(
        self,
        scenarios_dir: str | None = None,
        clab_dir: str | None = None,
        topology_metadata: dict[str, Any] | None = None,
        fault_scripts_dir: str | None = None,
    ):
        # Resolve workspace root
        package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        workspace_root = self._resolve_workspace_root(package_root)
        resolved_scenarios_dir = scenarios_dir or os.path.join(workspace_root, "scenarios")
        resolved_clab_dir = clab_dir or self._discover_topology_dir(workspace_root)

        # Build topology context
        topo_state = self._load_topology_state(topology_metadata, resolved_clab_dir)
        self._ctx = FaultContext.from_topology_state(
            topo_state,
            clab_dir=resolved_clab_dir,
            scenarios_dir=resolved_scenarios_dir,
        )

        # Build services (order matters — each layer depends on previous ones)
        self._cmd = CommandRunner()
        self._sonic = SonicRuntime(self._cmd, self._ctx)
        self._iface = InterfaceRuntime(self._cmd, self._sonic, self._ctx)
        self._topo_rt = TopologyRuntime(self._sonic, self._iface, self._ctx)
        self._routing = RoutingRuntime(self._topo_rt, self._ctx)
        self._tracker = FaultTracker()

        # Build handlers
        self._link = LinkHandler(self._cmd, self._sonic, self._iface, self._tracker, self._ctx)
        self._impairment = ImpairmentHandler(self._cmd, self._sonic, self._iface, self._tracker, self._ctx)
        self._bgp = BgpHandler(self._sonic, self._routing, self._tracker, self._ctx)
        self._static_route = StaticRouteHandler(self._sonic, self._tracker, self._ctx)
        self._route_policy = RoutePolicyHandler(self._sonic, self._routing, self._tracker, self._ctx)
        self._system = SystemHandler(self._cmd, self._sonic, self._iface, self._topo_rt, self._tracker, self._ctx)
        self._acl = AclHandler(self._cmd, self._sonic, self._routing, self._tracker, self._ctx)

    # ------------------------------------------------------------------
    # Bootstrap helpers
    # ------------------------------------------------------------------

    def _discover_topology_dir(self, base_dir: str) -> str:
        return discover_topology_dir(base_dir)

    @staticmethod
    def _resolve_workspace_root(package_root: str) -> str:
        env_root = config.workspace
        if env_root and os.path.isdir(env_root):
            return env_root
        repo_root = os.path.dirname(package_root)
        if os.path.isdir(os.path.join(repo_root, "scripts")) or os.path.isdir(os.path.join(repo_root, "scenarios")):
            return repo_root
        return package_root

    @staticmethod
    def _load_topology_state(topology_metadata, clab_dir):
        if topology_metadata:
            return build_topology_state_from_metadata(topology_metadata)
        metadata_file = os.path.join(clab_dir, "topology.json")
        if os.path.exists(metadata_file):
            with open(metadata_file, encoding="utf-8") as handle:
                return build_topology_state_from_metadata(json.load(handle))
        raise FileNotFoundError(
            f"Topology metadata not found: {metadata_file}. "
            "Pass topology_metadata=... or set NETOPSBENCH_TOPOLOGY_DIR to a generated topology directory."
        )

    # ------------------------------------------------------------------
    # Topology attribute properties (backward-compatible)
    # ------------------------------------------------------------------

    @property
    def topology_name(self) -> str:
        return self._ctx.topology_name

    @property
    def container_names(self) -> dict[str, str]:
        return self._ctx.container_names

    @container_names.setter
    def container_names(self, value: dict[str, str]):
        self._ctx.container_names = value

    @property
    def topology_metadata(self) -> dict[str, Any]:
        return self._ctx.topology_metadata

    @property
    def device_mgmt_ips(self) -> dict[str, str]:
        return self._ctx.device_mgmt_ips

    @property
    def clients(self) -> list[dict[str, Any]]:
        return self._ctx.clients

    @property
    def clients_by_leaf(self) -> dict[str, list[dict[str, Any]]]:
        return self._ctx.clients_by_leaf

    @property
    def scenarios_dir(self) -> str:
        return self._ctx.scenarios_dir

    @property
    def clab_dir(self) -> str:
        return self._ctx.clab_dir

    @property
    def active_faults(self) -> list:
        return self._tracker.active_faults

    @active_faults.setter
    def active_faults(self, value: list):
        self._tracker.active_faults = value

    # ------------------------------------------------------------------
    # Topology reload
    # ------------------------------------------------------------------

    def reload_topology(self, topology_metadata: dict[str, Any] = None, metadata_file: str = None) -> OperationResult:
        try:
            if topology_metadata:
                state = build_topology_state_from_metadata(topology_metadata)
            elif metadata_file:
                with open(metadata_file) as f:
                    state = build_topology_state_from_metadata(json.load(f))
            else:
                return OperationResult(
                    success=False, error="Either topology_metadata or metadata_file must be provided"
                )

            self._ctx.update_from_topology_state(state)
            return OperationResult(
                success=True,
                data={
                    "topology_name": self._ctx.topology_name,
                    "devices": list(self._ctx.container_names.keys()),
                    "total_devices": len(self._ctx.container_names),
                },
            )
        except Exception as e:
            return OperationResult(success=False, error=str(e))

    @staticmethod
    def _normalize_fault_result(payload: dict[str, Any] | OperationResult) -> OperationResult:
        if isinstance(payload, OperationResult):
            return payload
        data = dict(payload or {})
        if "success" in data:
            success = bool(data.pop("success"))
        elif "recovered" in data:
            success = bool(data.get("recovered"))
        else:
            success = not data.get("error")
        error = data.pop("error", None)
        return OperationResult(success=success, error=error, data=data)

    @classmethod
    def _legacy_fault_result(cls, payload: dict[str, Any] | OperationResult) -> dict[str, Any]:
        return cls._normalize_fault_result(payload).to_dict()

    # ------------------------------------------------------------------
    # Link fault delegation
    # ------------------------------------------------------------------

    def inject_link_down(self, device: str, interface: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._link.inject_link_down(device, interface))

    def recover_link_down(self, device: str, interface: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._link.recover_link_down(device, interface))

    def inject_link_flapping(self, device: str, interface: str, **kwargs) -> dict[str, Any]:
        return self._legacy_fault_result(self._link.inject_link_flapping(device, interface, **kwargs))

    # ------------------------------------------------------------------
    # Impairment fault delegation
    # ------------------------------------------------------------------

    def inject_mtu_mismatch(self, device: str, interface: str, mtu: int | None = None) -> dict[str, Any]:
        return self._legacy_fault_result(self._impairment.inject_mtu_mismatch(device, interface, mtu=mtu))

    def recover_mtu_mismatch(self, device: str, interface: str, original_mtu: int | None = None) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._impairment.recover_mtu_mismatch(device, interface, original_mtu=original_mtu)
        )

    def inject_packet_corruption(self, device: str, interface: str, corruption_pct: float = 5.0) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._impairment.inject_packet_corruption(device, interface, corruption_pct=corruption_pct)
        )

    def inject_packet_loss(self, device: str, interface: str, loss_pct: float = 10.0) -> dict[str, Any]:
        return self._legacy_fault_result(self._impairment.inject_packet_loss(device, interface, loss_pct=loss_pct))

    def inject_high_latency(self, device: str, interface: str, latency_ms: float = 100.0) -> dict[str, Any]:
        return self._legacy_fault_result(self._impairment.inject_high_latency(device, interface, latency_ms=latency_ms))

    def recover_tc_rules(self, device: str, interface: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._impairment.recover_tc_rules(device, interface))

    # ------------------------------------------------------------------
    # BGP fault delegation
    # ------------------------------------------------------------------

    def inject_bgp_neighbor_misconfig(self, device: str, **kwargs) -> dict[str, Any]:
        return self._legacy_fault_result(self._bgp.inject_bgp_neighbor_misconfig(device, **kwargs))

    def recover_bgp_neighbor_misconfig(
        self, device: str, peer_ip: str, misconfig_kind: str, **kwargs
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._bgp.recover_bgp_neighbor_misconfig(device, peer_ip, misconfig_kind, **kwargs)
        )

    # ------------------------------------------------------------------
    # Static route fault delegation
    # ------------------------------------------------------------------

    def inject_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._static_route.inject_blackhole_route(device, target_prefix))

    def recover_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._static_route.recover_blackhole_route(device, target_prefix))

    def inject_static_route_misconfig(
        self, device: str, target_ip: str | None = None, wrong_nexthop: str | None = None
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._static_route.inject_static_route_misconfig(device, target_ip=target_ip, wrong_nexthop=wrong_nexthop)
        )

    def recover_static_route_misconfig(
        self, device: str, target_ip: str, wrong_nexthop: str | None = None
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._static_route.recover_static_route_misconfig(device, target_ip, wrong_nexthop=wrong_nexthop)
        )

    # ------------------------------------------------------------------
    # Route policy fault delegation
    # ------------------------------------------------------------------

    def inject_route_policy_misconfig(self, device: str, **kwargs) -> dict[str, Any]:
        return self._legacy_fault_result(self._route_policy.inject_route_policy_misconfig(device, **kwargs))

    def recover_route_policy_misconfig(
        self, device: str, target_prefix: str, misconfig_kind: str, **kwargs
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._route_policy.recover_route_policy_misconfig(device, target_prefix, misconfig_kind, **kwargs)
        )

    # ------------------------------------------------------------------
    # ACL fault delegation
    # ------------------------------------------------------------------

    def inject_acl_misconfig(
        self, device: str, target_prefix: str | None = None, interface: str | None = None, direction: str = "in"
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._acl.inject_acl_misconfig(
                device, target_prefix=target_prefix, interface=interface, direction=direction
            )
        )

    def recover_acl_misconfig(
        self,
        device: str,
        target_prefix: str,
        interface: str | None = None,
        direction: str = "in",
        acl_name: str | None = None,
    ) -> dict[str, Any]:
        return self._legacy_fault_result(
            self._acl.recover_acl_misconfig(
                device, target_prefix, interface=interface, direction=direction, acl_name=acl_name
            )
        )

    # ------------------------------------------------------------------
    # System fault delegation
    # ------------------------------------------------------------------

    def inject_device_down(self, device: str) -> dict[str, Any]:
        return self._legacy_fault_result(self._system.inject_device_down(device))

    def recover_device_down(self, device: str, interfaces: list[str] | None = None) -> dict[str, Any]:
        return self._legacy_fault_result(self._system.recover_device_down(device, interfaces=interfaces))

    # ------------------------------------------------------------------
    # Recovery / query
    # ------------------------------------------------------------------

    def recover_all(self) -> list[dict[str, Any]]:
        """Attempt to recover from all active faults via the fault registry."""
        results = []
        remaining_faults = []

        for fault in list(self.active_faults):
            try:
                fault_type = fault["type"]
                spec = get_fault_spec(fault_type)
                if spec is None or spec.recover_active_fault is None:
                    result = {"type": fault_type, "recovered": False, "error": "Unknown fault type"}
                else:
                    result = spec.recover_active_fault(self, fault)
                result = self._legacy_fault_result(result)

                results.append(result)
                if not result.get("recovered", False):
                    remaining_faults.append(fault)

            except Exception as e:
                results.append({"type": fault.get("type"), "recovered": False, "error": str(e)})
                remaining_faults.append(fault)

        self.active_faults = remaining_faults
        return results

    def get_active_faults(self) -> list[dict[str, Any]]:
        """Get list of currently active faults."""
        return self._tracker.active_fault_dicts()
