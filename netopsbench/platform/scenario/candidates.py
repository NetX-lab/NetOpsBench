"""Shared candidate-path scope for offline placement and private effect audits."""

from collections import defaultdict, deque

from netopsbench.models.topology import DeviceRole
from netopsbench.platform.utils.interface_names import to_sonic_interface


class CandidatePaths:
    """Cache shortest-path DAG membership without enumerating complete paths."""

    def __init__(self, manifest):
        self.devices = {device.name: device for device in manifest.devices}
        self.clients = {device.name: device for device in manifest.clients()}
        self.adj, self.ports, self.distances = defaultdict(list), {}, {}
        for link in manifest.links:
            a, b = link.endpoints
            if self.devices[a.device].role != DeviceRole.CLIENT and self.devices[b.device].role != DeviceRole.CLIENT:
                self.adj[a.device].append(b.device)
                self.adj[b.device].append(a.device)
            self.ports[a.device, b.device] = to_sonic_interface(a.interface)
            self.ports[b.device, a.device] = to_sonic_interface(b.interface)
        self.pairs = [
            (client.name, dst) for client in manifest.require_pingmesh_plan().clients for dst in client.targets
        ]

    def distance(self, root):
        if root not in self.distances:
            distances, queue = {root: 0}, deque([root])
            while queue:
                node = queue.popleft()
                for neighbor in self.adj[node]:
                    if neighbor not in distances:
                        distances[neighbor] = distances[node] + 1
                        queue.append(neighbor)
            self.distances[root] = distances
        return self.distances[root]

    def contains(self, src, dst, root, peer=None):
        source, target = self.clients[src].attached_switch, self.clients[dst].attached_switch
        ds, dt = self.distance(source), self.distance(target)
        if peer in self.clients:
            return dst == peer and root == target
        if peer:
            return ds[root] + 1 + dt[peer] == ds[target]
        return ds[root] + dt[root] == ds[target]

    def affected_pairs(self, fault, root, peer, anchor, *, reverse=False):
        """Include all immutable planned pairs within this configuration's scope."""
        found = [anchor]
        for src, dst in self.pairs:
            if {"src": src, "dst": dst} == anchor:
                continue
            if fault == "acl_source_misconfig":
                if src != anchor["src"] and dst != anchor["src"]:
                    continue
            elif fault not in {
                "acl_l4_port_misconfig",
                "packet_loss",
                "packet_corruption",
                "high_latency",
                "mtu_mismatch",
            }:
                if dst != anchor["dst"] and src != anchor["dst"]:
                    continue
            forward, backward = self.contains(src, dst, root, peer), self.contains(dst, src, root, peer)
            if fault == "acl_source_misconfig":
                eligible = src == anchor["src"] and forward or dst == anchor["src"] and backward
            elif fault == "acl_l4_port_misconfig":
                eligible = forward
            elif fault in {"packet_loss", "packet_corruption", "high_latency", "mtu_mismatch"}:
                eligible = forward or backward
            elif fault == "routing_loop":
                eligible = (
                    dst == anchor["dst"]
                    and self.contains(src, dst, root)
                    or src == anchor["dst"]
                    and self.contains(dst, src, root)
                )
            else:
                member = self.contains(src, dst, peer, root) if reverse else forward
                returning = self.contains(dst, src, peer, root) if reverse else backward
                eligible = dst == anchor["dst"] and member or src == anchor["dst"] and returning
            if eligible:
                found.append({"src": src, "dst": dst})
        return found
