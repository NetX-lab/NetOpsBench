"""
Fault Injector - Programmatic interface for injecting network faults.

Supports various fault types for DCN troubleshooting benchmark.
"""

from pathlib import Path
from typing import Any

from netopsbench.platform.faults.context import FaultRuntimeContext
from netopsbench.platform.faults.handlers.acl import AclHandler
from netopsbench.platform.faults.handlers.impairment import ImpairmentHandler
from netopsbench.platform.faults.handlers.link import LinkHandler
from netopsbench.platform.faults.handlers.routing_bgp import BgpHandler
from netopsbench.platform.faults.handlers.routing_policy import RoutePolicyHandler
from netopsbench.platform.faults.handlers.routing_static import StaticRouteHandler
from netopsbench.platform.faults.handlers.system import SystemHandler
from netopsbench.platform.faults.services.command_runner import CommandRunner
from netopsbench.platform.faults.services.interface_runtime import InterfaceRuntime
from netopsbench.platform.faults.services.routing_runtime import RoutingRuntime
from netopsbench.platform.faults.services.sonic_runtime import SonicRuntime
from netopsbench.platform.faults.services.topology_runtime import TopologyRuntime
from netopsbench.platform.faults.services.tracking import FaultTracker
from netopsbench.platform.faults.specs import FaultSpecRegistry, create_fault_registry
from netopsbench.platform.topology.topology_utils import (
    coerce_topology_manifest,
    load_topology_manifest,
)


class FaultInjector:
    """
    Manages fault injection for network troubleshooting benchmarks.

    Provides methods to inject and recover from various network faults.
    Uses composition: each concern lives in a dedicated service or handler class.
    """

    def __init__(
        self,
        clab_dir: str | None = None,
        topology_metadata: dict[str, Any] | None = None,
        fault_registry: FaultSpecRegistry | None = None,
    ):
        self.fault_registry = fault_registry or create_fault_registry()
        resolved_clab_dir = Path(clab_dir or ".").resolve()
        if topology_metadata is not None:
            manifest = coerce_topology_manifest(topology_metadata)
        else:
            metadata_file = resolved_clab_dir / "topology.json"
            if not metadata_file.is_file():
                raise FileNotFoundError(
                    f"Topology metadata not found: {metadata_file}. "
                    "Pass topology_metadata or a generated topology directory."
                )
            manifest = load_topology_manifest(metadata_file)
        self._ctx = FaultRuntimeContext(manifest=manifest, clab_dir=resolved_clab_dir)

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

    @property
    def topology_name(self) -> str:
        return self._ctx.topology_name

    @property
    def container_names(self) -> dict[str, str]:
        return self._ctx.container_names

    @property
    def topology_metadata(self) -> dict[str, Any]:
        return self._ctx.topology_metadata

    @property
    def clab_dir(self) -> str:
        return str(self._ctx.clab_dir)

    @property
    def active_faults(self) -> list:
        return self._tracker.active_faults

    @active_faults.setter
    def active_faults(self, value: list):
        self._tracker.active_faults = value

    def track_active_fault(self, fault_info: dict[str, Any]) -> None:
        """Record a successfully injected registry fault in canonical form."""
        self._tracker.track(fault_info)

    # ------------------------------------------------------------------
    # Link fault delegation
    # ------------------------------------------------------------------

    def inject_link_down(self, device: str, interface: str) -> dict[str, Any]:
        return self._link.inject_link_down(device, interface)

    def recover_link_down(self, device: str, interface: str) -> dict[str, Any]:
        return self._link.recover_link_down(device, interface)

    def inject_link_flapping(self, device: str, interface: str, **kwargs) -> dict[str, Any]:
        return self._link.inject_link_flapping(device, interface, **kwargs)

    # ------------------------------------------------------------------
    # Impairment fault delegation
    # ------------------------------------------------------------------

    def inject_mtu_mismatch(self, device: str, interface: str, mtu: int | None = None) -> dict[str, Any]:
        return self._impairment.inject_mtu_mismatch(device, interface, mtu=1400 if mtu is None else mtu)

    def recover_mtu_mismatch(self, device: str, interface: str, original_mtu: int | None = None) -> dict[str, Any]:
        return self._impairment.recover_mtu_mismatch(device, interface, original_mtu=original_mtu)

    def inject_packet_corruption(self, device: str, interface: str, corruption_pct: float = 5.0) -> dict[str, Any]:
        return self._impairment.inject_packet_corruption(device, interface, corruption_pct=corruption_pct)

    def inject_packet_loss(self, device: str, interface: str, loss_pct: float = 10.0) -> dict[str, Any]:
        return self._impairment.inject_packet_loss(device, interface, loss_pct=loss_pct)

    def inject_high_latency(self, device: str, interface: str, latency_ms: float = 100.0) -> dict[str, Any]:
        return self._impairment.inject_high_latency(device, interface, latency_ms=latency_ms)

    def recover_tc_rules(self, device: str, interface: str) -> dict[str, Any]:
        return self._impairment.recover_tc_rules(device, interface)

    # ------------------------------------------------------------------
    # BGP fault delegation
    # ------------------------------------------------------------------

    def inject_bgp_neighbor_misconfig(self, device: str, **kwargs) -> dict[str, Any]:
        return self._bgp.inject_bgp_neighbor_misconfig(device, **kwargs)

    def recover_bgp_neighbor_misconfig(
        self, device: str, peer_ip: str, misconfig_kind: str, **kwargs
    ) -> dict[str, Any]:
        return self._bgp.recover_bgp_neighbor_misconfig(device, peer_ip, misconfig_kind, **kwargs)

    # ------------------------------------------------------------------
    # Static route fault delegation
    # ------------------------------------------------------------------

    def inject_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        return self._static_route.inject_blackhole_route(device, target_prefix)

    def recover_blackhole_route(self, device: str, target_prefix: str) -> dict[str, Any]:
        return self._static_route.recover_blackhole_route(device, target_prefix)

    def inject_static_route_misconfig(
        self, device: str, target_ip: str | None = None, wrong_nexthop: str | None = None
    ) -> dict[str, Any]:
        return self._static_route.inject_static_route_misconfig(
            device, target_ip=target_ip, wrong_nexthop=wrong_nexthop
        )

    def recover_static_route_misconfig(
        self, device: str, target_ip: str, wrong_nexthop: str | None = None
    ) -> dict[str, Any]:
        return self._static_route.recover_static_route_misconfig(device, target_ip, wrong_nexthop=wrong_nexthop)

    # ------------------------------------------------------------------
    # Route policy fault delegation
    # ------------------------------------------------------------------

    def inject_route_policy_misconfig(self, device: str, **kwargs) -> dict[str, Any]:
        return self._route_policy.inject_route_policy_misconfig(device, **kwargs)

    def recover_route_policy_misconfig(
        self, device: str, target_prefix: str, misconfig_kind: str, **kwargs
    ) -> dict[str, Any]:
        return self._route_policy.recover_route_policy_misconfig(device, target_prefix, misconfig_kind, **kwargs)

    # ------------------------------------------------------------------
    # ACL fault delegation
    # ------------------------------------------------------------------

    def inject_acl_misconfig(
        self, device: str, target_prefix: str | None = None, interface: str | None = None, direction: str = "in"
    ) -> dict[str, Any]:
        return self._acl.inject_acl_misconfig(
            device, target_prefix=target_prefix, interface=interface, direction=direction
        )

    def recover_acl_misconfig(
        self,
        device: str,
        target_prefix: str,
        interface: str | None = None,
        direction: str = "in",
        acl_name: str | None = None,
    ) -> dict[str, Any]:
        return self._acl.recover_acl_misconfig(
            device, target_prefix, interface=interface, direction=direction, acl_name=acl_name
        )

    # ------------------------------------------------------------------
    # System fault delegation
    # ------------------------------------------------------------------

    def inject_device_down(self, device: str) -> dict[str, Any]:
        return self._system.inject_device_down(device)

    def recover_device_down(self, device: str, interfaces: list[str] | None = None) -> dict[str, Any]:
        return self._system.recover_device_down(device, interfaces=interfaces)

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
                spec = self.fault_registry.get(fault_type)
                if spec is None or spec.recover_active_fault is None:
                    result = {"type": fault_type, "recovered": False, "error": "Unknown fault type"}
                else:
                    result = spec.recover_active_fault(self, fault)
                results.append(result)
                if not result.get("recovered", False):
                    remaining_faults.append(fault)

            except Exception as e:
                results.append({"type": fault.get("type"), "recovered": False, "error": str(e)})
                remaining_faults.append(fault)

        self.active_faults = remaining_faults
        return results
