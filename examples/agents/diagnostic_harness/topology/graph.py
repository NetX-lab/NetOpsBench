"""Physical topology graph and bounded ECMP path enumeration."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from netopsbench.models.topology import TopologyManifest
from ..context_topology import load_context_manifest
from ..normalization.interface import PhysicalLink, TopologyIndex

PathSet = list[list[str]]


@dataclass(frozen=True)
class ShortestPathProfile:
    paths: PathSet
    total_paths: int
    link_fractions: dict[str, float]

    @property
    def truncated(self) -> bool:
        return self.total_paths > len(self.paths)


@dataclass(frozen=True)
class TopologyGraphLink:
    physical: PhysicalLink
    role: str
    mtu: int | None = None

    @property
    def link_id(self) -> str:
        return self.physical.link_id


class TopologyGraph:
    """Undirected physical graph with address and attachment indexes."""

    def __init__(
        self,
        *,
        index: TopologyIndex,
        links: tuple[TopologyGraphLink, ...],
        addresses: Mapping[str, str] | None = None,
        attachments: Mapping[str, str] | None = None,
    ):
        self.index = index
        self.links = links
        self.roles = dict(index.devices)
        self.addresses = {str(address): str(device) for address, device in (addresses or {}).items()}
        self.attachments = dict(attachments or {})
        self._links_by_id = {link.link_id: link for link in links}
        self._adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for link in links:
            left = link.physical.endpoint_a.device
            right = link.physical.endpoint_b.device
            self._adjacency[left].append((right, link.link_id))
            self._adjacency[right].append((left, link.link_id))
        for neighbors in self._adjacency.values():
            neighbors.sort()

    @classmethod
    def from_manifest(cls, manifest: TopologyManifest, *, source: str = "topology_manifest") -> TopologyGraph:
        index = TopologyIndex.from_manifest(manifest, source=source)
        persisted = {frozenset(endpoint.device for endpoint in link.endpoints): link for link in manifest.links}
        links: list[TopologyGraphLink] = []
        for physical in index.links:
            key = frozenset((physical.endpoint_a.device, physical.endpoint_b.device))
            raw = persisted[key]
            links.append(
                TopologyGraphLink(
                    physical=physical,
                    role=str(raw.kind),
                    mtu=int(raw.mtu) if raw.mtu is not None else None,
                )
            )
        addresses: dict[str, str] = {}
        attachments: dict[str, str] = {}
        for device in manifest.devices:
            if device.data_ip:
                addresses[str(device.data_ip)] = device.name
            if device.attached_switch:
                attachments[device.name] = device.attached_switch
        return cls(index=index, links=tuple(links), addresses=addresses, attachments=attachments)

    @classmethod
    def from_context(cls, context: Any) -> TopologyGraph:
        manifest, source = load_context_manifest(context)
        if manifest is None:
            raise ValueError("diagnostic context does not contain a valid topology manifest")
        return cls.from_manifest(manifest, source=source or "context_manifest")

    def link(self, link_id: str) -> TopologyGraphLink | None:
        return self._links_by_id.get(link_id)

    def resolve_node(self, value: str | None) -> str | None:
        if not value:
            return None
        text = str(value)
        resolved = self.index.resolve_device(text)
        return resolved or self.addresses.get(text)

    def endpoint_link(self, device: str, interface: str) -> TopologyGraphLink | None:
        physical = self.index.physical_link(device, interface)
        return self.link(physical.link_id) if physical else None

    def network_links(self) -> tuple[TopologyGraphLink, ...]:
        return tuple(
            link
            for link in self.links
            if self.roles.get(link.physical.endpoint_a.device) != "client"
            and self.roles.get(link.physical.endpoint_b.device) != "client"
        )

    def attachment_devices(self) -> tuple[str, ...]:
        return tuple(sorted(self.index.attachment_devices))

    def is_attachment_device(self, device: str | None) -> bool:
        return self.index.is_attachment_device(device)

    def fabric_links(self, device: str | None = None) -> tuple[TopologyGraphLink, ...]:
        resolved = self.resolve_node(device) if device else None
        return tuple(
            link
            for link in self.network_links()
            if resolved is None or resolved in {link.physical.endpoint_a.device, link.physical.endpoint_b.device}
        )

    def fabric_neighbors(self, device: str) -> tuple[str, ...]:
        resolved = self.resolve_node(device)
        if resolved is None:
            return ()
        network_link_ids = {link.link_id for link in self.network_links()}
        return tuple(sorted(peer for peer, link_id in self._adjacency.get(resolved, ()) if link_id in network_link_ids))

    def max_attachment_fabric_degree(self) -> int:
        """Largest client-attachment uplink fanout in the current inventory."""
        return max(
            (len(self.fabric_neighbors(device)) for device in self.attachment_devices()),
            default=0,
        )

    def shortest_path_network_link_union(self, source: str, destination: str) -> frozenset[str]:
        """Return every network link that belongs to any shortest path."""
        start = self.resolve_node(source)
        end = self.resolve_node(destination)
        if start is None or end is None or start == end:
            return frozenset()
        distances_from_start = self._distances(start)
        distances_from_end = self._distances(end)
        return self._shortest_path_network_link_union_from_distances(
            start,
            end,
            distances_from_start,
            distances_from_end,
        )

    def max_attachment_path_failure_domain_size(self) -> int:
        """Estimate the widest local path domain without enumerating ECMP paths.

        For each attachment, inspect deterministic representatives among the
        farthest attachment devices.  Built-in Clos and fat-tree fabrics are
        symmetric, while checking both lexical ends avoids making the result
        depend on one arbitrary peer in less regular inventories.
        """
        attachments = self.attachment_devices()
        if len(attachments) < 2:
            return 0
        distances = {device: self._distances(device) for device in attachments}
        maximum = 0
        for source in attachments:
            reachable = [
                destination
                for destination in attachments
                if destination != source and destination in distances[source]
            ]
            if not reachable:
                continue
            farthest_distance = max(distances[source][destination] for destination in reachable)
            farthest = sorted(
                destination
                for destination in reachable
                if distances[source][destination] == farthest_distance
            )
            for destination in dict.fromkeys((farthest[0], farthest[-1])):
                maximum = max(
                    maximum,
                    len(
                        self._shortest_path_network_link_union_from_distances(
                            source,
                            destination,
                            distances[source],
                            distances[destination],
                        )
                    ),
                )
        return maximum

    def _shortest_path_network_link_union_from_distances(
        self,
        source: str,
        destination: str,
        distances_from_source: Mapping[str, int],
        distances_from_destination: Mapping[str, int],
    ) -> frozenset[str]:
        shortest_distance = distances_from_source.get(destination)
        if shortest_distance is None:
            return frozenset()
        members: set[str] = set()
        for link in self.network_links():
            left = link.physical.endpoint_a.device
            right = link.physical.endpoint_b.device
            forward = (
                distances_from_source.get(left),
                distances_from_destination.get(right),
            )
            reverse = (
                distances_from_source.get(right),
                distances_from_destination.get(left),
            )
            if (
                None not in forward and int(forward[0]) + 1 + int(forward[1]) == shortest_distance
            ) or (
                None not in reverse and int(reverse[0]) + 1 + int(reverse[1]) == shortest_distance
            ):
                members.add(link.link_id)
        return frozenset(members)

    def _distances(self, start: str) -> dict[str, int]:
        distances = {start: 0}
        queue = deque((start,))
        while queue:
            node = queue.popleft()
            for neighbor, _link_id in self._adjacency.get(node, ()):
                if neighbor in distances:
                    continue
                distances[neighbor] = distances[node] + 1
                queue.append(neighbor)
        return distances

    def probe_frontier_size(self, configured: int, *, maximum: int) -> int:
        """Bound a degree-aware probe frontier independently of server count.

        The frontier grows with the local ECMP failure domain only.  It never
        grows with the number of unrelated leaves or clients, so the same
        policy is suitable for small fabrics and much larger clusters.
        """
        return min(
            max(0, int(maximum)),
            max(0, int(configured), self.max_attachment_fabric_degree()),
        )

    def failure_domain_probe_frontier_size(self, configured: int, *, maximum: int) -> int:
        """Size a probe frontier for both ends of one affected path.

        A leaf-to-leaf symptom can originate on either attachment switch's
        ECMP cut.  Scale with that local two-ended failure domain, not with the
        number of unrelated devices in the fabric.
        """
        return min(
            max(0, int(maximum)),
            max(0, int(configured), self.max_attachment_fabric_degree() * 2),
        )

    def diagnosable_links(self) -> tuple[TopologyGraphLink, ...]:
        """Return physical links with at least one network-device endpoint."""
        return tuple(
            link
            for link in self.links
            if self.roles.get(link.physical.endpoint_a.device) != "client"
            or self.roles.get(link.physical.endpoint_b.device) != "client"
        )

    def all_shortest_paths(self, source: str, destination: str, *, max_paths: int = 32) -> PathSet:
        return self.shortest_path_profile(source, destination, max_paths=max_paths).paths

    def shortest_path_profile(
        self,
        source: str,
        destination: str,
        *,
        max_paths: int = 32,
    ) -> ShortestPathProfile:
        """Return bounded path samples plus exact ECMP link membership.

        Link fractions are computed over the shortest-path DAG, so ranking is
        unbiased even when a large fabric has more ECMP paths than the trace
        sample limit.
        """
        start = self.resolve_node(source)
        end = self.resolve_node(destination)
        if start is None or end is None or start == end:
            return ShortestPathProfile([], 0, {})
        distance = {start: 0}
        predecessors: dict[str, list[tuple[str, str]]] = defaultdict(list)
        queue = deque((start,))
        while queue:
            node = queue.popleft()
            for neighbor, link_id in self._adjacency.get(node, ()):
                candidate = distance[node] + 1
                if neighbor not in distance:
                    distance[neighbor] = candidate
                    predecessors[neighbor].append((node, link_id))
                    queue.append(neighbor)
                elif distance[neighbor] == candidate:
                    predecessors[neighbor].append((node, link_id))
        if end not in distance:
            return ShortestPathProfile([], 0, {})

        successors: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for node, entries in predecessors.items():
            for predecessor, link_id in entries:
                successors[predecessor].append((node, link_id))
        for entries in successors.values():
            entries.sort()

        paths_from: dict[str, int] = {start: 1}
        for node in sorted(distance, key=lambda item: (distance[item], item)):
            for successor, _link_id in successors.get(node, ()):
                paths_from[successor] = paths_from.get(successor, 0) + paths_from.get(node, 0)
        paths_to: dict[str, int] = {end: 1}
        for node in sorted(distance, key=lambda item: (-distance[item], item)):
            if node == end:
                continue
            paths_to[node] = sum(paths_to.get(successor, 0) for successor, _ in successors.get(node, ()))
        total_paths = paths_from.get(end, 0)
        link_fractions: dict[str, float] = {}
        if total_paths:
            for node, entries in successors.items():
                for successor, link_id in entries:
                    count = paths_from.get(node, 0) * paths_to.get(successor, 0)
                    if count:
                        link_fractions[link_id] = count / total_paths
        paths: PathSet = []

        def walk(node: str, reversed_links: list[str]) -> None:
            if len(paths) >= max(0, max_paths):
                return
            if node == start:
                paths.append(list(reversed(reversed_links)))
                return
            for predecessor, link_id in sorted(predecessors[node]):
                walk(predecessor, [*reversed_links, link_id])

        walk(end, [])
        return ShortestPathProfile(paths, total_paths, link_fractions)


__all__ = ["PathSet", "ShortestPathProfile", "TopologyGraph", "TopologyGraphLink"]
