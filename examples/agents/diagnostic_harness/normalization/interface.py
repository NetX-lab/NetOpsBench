"""Manifest-backed interface and physical-peer normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.utils.interface_names import interface_aliases, to_sonic_interface

from ..context_topology import load_context_manifest
from ..models import InterfaceNormalization


def _lookup_key(value: str | None) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class LinkEndpoint:
    device: str
    canonical_interface: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class PhysicalLink:
    link_id: str
    endpoint_a: LinkEndpoint
    endpoint_b: LinkEndpoint

    def peer_of(self, device: str, interface: str) -> LinkEndpoint | None:
        target = (_lookup_key(device), _lookup_key(interface))
        if target == (_lookup_key(self.endpoint_a.device), _lookup_key(self.endpoint_a.canonical_interface)):
            return self.endpoint_b
        if target == (_lookup_key(self.endpoint_b.device), _lookup_key(self.endpoint_b.canonical_interface)):
            return self.endpoint_a
        return None


class TopologyIndex:
    """Device, interface, and physical-link index derived from inventory."""

    def __init__(
        self,
        *,
        devices: Mapping[str, str],
        links: tuple[PhysicalLink, ...] = (),
        attachment_devices: tuple[str, ...] = (),
        source: str = "context_projection",
    ):
        self.devices = dict(devices)
        self.links = links
        self.source = source
        explicit_attachments = {str(device) for device in attachment_devices if str(device) in self.devices}
        # Keep explicit inventory attachments for arbitrary real-world role
        # names, and union canonical roles so a partial client projection does
        # not silently hide otherwise valid leaf/edge switches.
        self.attachment_devices = frozenset(
            explicit_attachments | {device for device, role in self.devices.items() if role in {"leaf", "edge"}}
        )
        self._device_names = {_lookup_key(name): name for name in self.devices}
        self._interfaces: dict[tuple[str, str], LinkEndpoint] = {}
        self._links_by_endpoint: dict[tuple[str, str], PhysicalLink] = {}
        for link in links:
            for endpoint in (link.endpoint_a, link.endpoint_b):
                device_key = _lookup_key(endpoint.device)
                for alias in endpoint.aliases:
                    self._interfaces[(device_key, _lookup_key(alias))] = endpoint
                self._interfaces[(device_key, _lookup_key(endpoint.canonical_interface))] = endpoint
                self._links_by_endpoint[(device_key, _lookup_key(endpoint.canonical_interface))] = link

    @property
    def interface_mapping_available(self) -> bool:
        return bool(self.links)

    def resolve_device(self, device: str | None) -> str | None:
        if not device:
            return None
        return self._device_names.get(_lookup_key(device))

    def resolve_interface(self, device: str, interface: str) -> LinkEndpoint | None:
        resolved_device = self.resolve_device(device)
        if not resolved_device:
            return None
        return self._interfaces.get((_lookup_key(resolved_device), _lookup_key(interface)))

    def is_client(self, device: str | None) -> bool:
        resolved = self.resolve_device(device)
        return bool(resolved and self.devices.get(resolved) == DeviceRole.CLIENT.value)

    def is_attachment_device(self, device: str | None) -> bool:
        """Whether a switch directly owns client-facing links.

        This functional role maps to a CLOS leaf, a fat-tree edge, or any
        future inventory role referenced by a client's ``attached_switch``.
        """
        resolved = self.resolve_device(device)
        return bool(resolved and resolved in self.attachment_devices)

    def routing_devices(self) -> tuple[str, ...]:
        return tuple(sorted(device for device, role in self.devices.items() if role != DeviceRole.CLIENT.value))

    def transit_devices(self) -> tuple[str, ...]:
        return tuple(device for device in self.routing_devices() if device not in self.attachment_devices)

    def network_neighbors(self, device: str | None) -> tuple[str, ...]:
        resolved = self.resolve_device(device)
        if resolved is None:
            return ()
        neighbors: set[str] = set()
        for link in self.links:
            endpoints = (link.endpoint_a.device, link.endpoint_b.device)
            if resolved not in endpoints:
                continue
            peer = endpoints[1] if endpoints[0] == resolved else endpoints[0]
            if not self.is_client(peer):
                neighbors.add(peer)
        return tuple(sorted(neighbors))

    def routing_scope(self, seed: str | None = None, *, limit: int = 4) -> tuple[str, ...]:
        """Return a bounded, topology-local routing-device search order."""
        maximum = max(0, int(limit))
        if maximum == 0:
            return ()
        resolved = self.resolve_device(seed)
        if resolved is None or self.is_client(resolved):
            ordered = [*sorted(self.attachment_devices), *self.transit_devices()]
            return tuple(dict.fromkeys(ordered))[:maximum]
        if not self.links:
            # Some replay/public contexts expose role inventory without
            # physical links. Search transit tiers before the endpoint switch;
            # role names remain inventory-defined (spine, agg/core, or other).
            ordered = [*self.transit_devices(), resolved, *sorted(self.attachment_devices)]
            return tuple(dict.fromkeys(ordered))[:maximum]
        ordered: list[str] = []
        queue = [resolved]
        seen = {resolved}
        while queue and len(ordered) < maximum:
            current = queue.pop(0)
            ordered.append(current)
            for neighbor in self.network_neighbors(current):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        return tuple(ordered)

    def physical_link(self, device: str, interface: str) -> PhysicalLink | None:
        endpoint = self.resolve_interface(device, interface)
        if endpoint is None:
            return None
        return self._links_by_endpoint.get((_lookup_key(endpoint.device), _lookup_key(endpoint.canonical_interface)))

    def peer(self, device: str, interface: str) -> LinkEndpoint | None:
        endpoint = self.resolve_interface(device, interface)
        link = self.physical_link(device, interface)
        if endpoint is None or link is None:
            return None
        return link.peer_of(endpoint.device, endpoint.canonical_interface)

    @classmethod
    def from_manifest(cls, manifest: TopologyManifest, *, source: str = "topology_manifest") -> TopologyIndex:
        roles = {device.name: device.role.value for device in manifest.devices}
        attachment_devices = tuple(
            sorted({client.attached_switch for client in manifest.clients() if client.attached_switch})
        )
        links: list[PhysicalLink] = []
        for persisted_link in manifest.links:
            endpoints: list[LinkEndpoint] = []
            for raw_endpoint in persisted_link.endpoints:
                role = roles[raw_endpoint.device]
                canonical = (
                    raw_endpoint.interface
                    if role == DeviceRole.CLIENT.value
                    else to_sonic_interface(raw_endpoint.interface)
                )
                aliases = tuple(sorted(interface_aliases(raw_endpoint.interface) | {canonical}, key=str.lower))
                endpoints.append(
                    LinkEndpoint(
                        device=raw_endpoint.device,
                        canonical_interface=canonical,
                        aliases=aliases,
                    )
                )
            endpoint_a, endpoint_b = endpoints
            link_id = (
                f"{endpoint_a.device}:{endpoint_a.canonical_interface}--"
                f"{endpoint_b.device}:{endpoint_b.canonical_interface}"
            )
            links.append(PhysicalLink(link_id=link_id, endpoint_a=endpoint_a, endpoint_b=endpoint_b))
        return cls(
            devices=roles,
            links=tuple(links),
            attachment_devices=attachment_devices,
            source=source,
        )

    @classmethod
    def from_mapping(cls, topology: Mapping[str, Any] | None) -> TopologyIndex:
        payload = dict(topology or {})
        try:
            manifest = TopologyManifest.model_validate(payload)
        except (TypeError, ValueError):
            manifest = None
        if manifest is not None:
            return cls.from_manifest(manifest, source="context_manifest")

        devices: dict[str, str] = {}
        attachment_devices: set[str] = set()
        raw_devices = payload.get("devices")
        if isinstance(raw_devices, Mapping):
            for group, entries in raw_devices.items():
                role = str(group).rstrip("s")
                for entry in entries if isinstance(entries, list) else []:
                    if isinstance(entry, Mapping) and entry.get("name"):
                        devices[str(entry["name"])] = str(entry.get("role") or role)
                        if role == DeviceRole.CLIENT.value:
                            attached = entry.get("attached_switch") or entry.get("leaf") or entry.get("edge")
                            if attached:
                                attachment_devices.add(str(attached))
                    elif isinstance(entry, str):
                        devices[entry] = role
        elif isinstance(raw_devices, list):
            for entry in raw_devices:
                if isinstance(entry, Mapping) and entry.get("name"):
                    devices[str(entry["name"])] = str(entry.get("role") or "unknown")
                    if str(entry.get("role") or "") == DeviceRole.CLIENT.value:
                        attached = entry.get("attached_switch") or entry.get("leaf") or entry.get("edge")
                        if attached:
                            attachment_devices.add(str(attached))
        return cls(
            devices=devices,
            attachment_devices=tuple(sorted(attachment_devices)),
            source="context_projection",
        )

    @classmethod
    def from_context(cls, context: Any) -> TopologyIndex:
        manifest, source = load_context_manifest(context)
        if manifest is not None:
            return cls.from_manifest(manifest, source=source or "context_manifest")
        return cls.from_mapping(getattr(context, "topology", {}) or {})


class InterfaceNameNormalizer:
    def __init__(self, topology: TopologyIndex):
        self.topology = topology

    def normalize(self, device: str | None, interface: str | None) -> InterfaceNormalization:
        if interface is None or not str(interface).strip():
            return InterfaceNormalization(device=device, original=interface, value=None)
        if not device:
            return InterfaceNormalization(
                device=device,
                original=str(interface),
                value=str(interface),
                validation_error="interface cannot be resolved without a device",
            )
        resolved_device = self.topology.resolve_device(device)
        if resolved_device is None:
            return InterfaceNormalization(
                device=device,
                original=str(interface),
                value=str(interface),
                validation_error=f"unknown topology device: {device}",
            )
        if not self.topology.interface_mapping_available:
            return InterfaceNormalization(
                device=resolved_device,
                original=str(interface),
                value=str(interface),
                validation_error="canonical topology interface mapping is unavailable",
            )
        endpoint = self.topology.resolve_interface(resolved_device, str(interface))
        if endpoint is None:
            return InterfaceNormalization(
                device=resolved_device,
                original=str(interface),
                value=str(interface),
                validation_error=f"interface {interface} does not belong to {resolved_device}",
            )
        link = self.topology.physical_link(endpoint.device, endpoint.canonical_interface)
        return InterfaceNormalization(
            device=endpoint.device,
            original=str(interface),
            value=endpoint.canonical_interface,
            link_id=link.link_id if link else None,
        )


__all__ = ["InterfaceNameNormalizer", "LinkEndpoint", "PhysicalLink", "TopologyIndex"]
