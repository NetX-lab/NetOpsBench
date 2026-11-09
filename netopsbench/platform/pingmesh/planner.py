"""The sole compiler for peer selection, probe schedules and candidate coverage."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from itertools import combinations, permutations

from netopsbench.models.pingmesh import (
    CandidateCoverage,
    ClientProbePlan,
    LinkCoverage,
    PingmeshPlan,
    PlanSettings,
    ProbeFrame,
)
from netopsbench.models.topology import TopologyManifest


def plan_fingerprint(manifest: TopologyManifest, plan: PingmeshPlan) -> str:
    payload = plan.model_dump(mode="json")
    payload["settings"]["plan_id"] = ""
    encoded = json.dumps(
        {"topology": manifest.model_dump(mode="json", exclude={"pingmesh_plan"}), "plan": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compile_pingmesh_plan(manifest: TopologyManifest) -> PingmeshPlan:
    policy = manifest.pingmesh
    clients = manifest.clients()
    if len(clients) < 2:
        raise ValueError("Pingmesh requires at least two clients")
    racks: dict[str, list[str]] = {}
    for client in clients:
        racks.setdefault(str(client.attached_switch), []).append(client.name)
    peers: dict[str, set[str]] = {client.name: set() for client in clients}
    rack_pair_counts: Counter[tuple[str, str]] = Counter()
    for names in racks.values():
        for source, destination in permutations(names, 2):
            peers[source].add(destination)
    indexed_racks = list(enumerate(racks.items()))
    for (i, (left, a)), (j, (right, b)) in combinations(indexed_racks, 2):
        count = policy.cross_rack_pairs or max(len(a), len(b))
        if count > max(len(a), len(b)):
            raise ValueError("cross_rack_pairs exceeds the rack matching capacity")
        for replica in range(count):
            source = a[(i + j + replica) % len(a)]
            destination = b[(i + j + replica) % len(b)]
            peers[source].add(destination)
            peers[destination].add(source)
        rack_pair_counts[left, right] = count
        rack_pair_counts[right, left] = count
    if any(not targets for targets in peers.values()):
        raise ValueError("Pingmesh plan leaves a client without probes")

    order = {client.name: index for index, client in enumerate(clients)}
    batch_size = policy.destination_batch_size or max(map(len, peers.values()))
    destination_batches = max((len(names) + batch_size - 1) // batch_size for names in peers.values())
    if policy.rtt_ports_per_cycle > policy.rtt_port_pool_size:
        raise ValueError("rtt_ports_per_cycle exceeds the source-port pool")
    source_ports = list(range(33000, 33000 + policy.rtt_port_pool_size))
    if source_ports[-1] > 65535 or 33434 in source_ports:
        raise ValueError("Pingmesh source-port pool overflows or overlaps the responder")
    port_batches = [
        list(range(start, min(start + policy.rtt_ports_per_cycle, len(source_ports))))
        for start in range(0, len(source_ports), policy.rtt_ports_per_cycle)
    ]
    epoch = destination_batches * len(port_batches)
    interval = policy.cycle_interval_seconds
    plans = []
    for client in clients:
        targets = sorted(peers[client.name], key=order.__getitem__)
        frames = []
        for port_batch, sockets in enumerate(port_batches):
            for destination_batch in range(destination_batches):
                active = list(
                    range(destination_batch * batch_size, min((destination_batch + 1) * batch_size, len(targets)))
                )
                probes = []
                for target in range(len(active)):
                    probes.extend(("rtt", target, socket) for socket in range(len(sockets)))
                    probes.append(("df", target, target % len(sockets)))
                frames.append(
                    ProbeFrame(
                        destination_batch_index=destination_batch,
                        port_batch_index=port_batch,
                        target_indices=active,
                        probes=probes,
                    )
                )
        jitter = int.from_bytes(hashlib.sha256(client.name.encode()).digest()[:8], "big") % (interval * 1000)
        plans.append(ClientProbePlan(name=client.name, targets=targets, startup_delay_ms=jitter, frames=frames))
    plan = PingmeshPlan(
        settings=PlanSettings(
            plan_id="",
            source_ports=source_ports,
            port_batches=port_batches,
            destination_batch_size=batch_size,
            cycle_interval_seconds=interval,
            coverage_epoch_cycles=epoch,
            coverage_epoch_seconds=epoch * interval,
            complete_window_seconds=epoch * interval + max(5, 2 * interval),
            df_payload_size=policy.df_payload_size,
        ),
        clients=plans,
        coverage=_candidate_coverage(manifest, racks, peers, rack_pair_counts),
    )
    if set(plan.coverage.covered_switches) != {device.name for device in manifest.switches()}:
        raise ValueError("Pingmesh plan leaves switches without candidate coverage")
    if plan.coverage.minimum_witnesses < (2 if manifest.scale in {"xlarge", "fat-tree-k18"} else 1):
        raise ValueError("Pingmesh plan has insufficient candidate link coverage")
    plan.settings.plan_id = plan_fingerprint(manifest, plan)
    return plan


def _candidate_coverage(manifest, racks, peers, rack_pair_counts) -> CandidateCoverage:
    switches = {device.name for device in manifest.switches()}
    graph: dict[str, list[str]] = {name: [] for name in switches}
    for link in manifest.links:
        a, b = (endpoint.device for endpoint in link.endpoints)
        if a in switches and b in switches:
            graph[a].append(b)
            graph[b].append(a)
    witnesses: Counter[tuple[str, str]] = Counter()
    # Traverse shortest-path predecessor DAGs, never enumerate complete paths.
    for source in racks:
        distances = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for neighbor in graph[node]:
                if neighbor not in distances:
                    distances[neighbor] = distances[node] + 1
                    queue.append(neighbor)
        for destination in racks:
            count = rack_pair_counts[source, destination]
            if not count:
                continue
            if destination not in distances:
                raise ValueError(f"Disconnected racks: {source}, {destination}")
            pending = [destination]
            visited = {destination}
            while pending:
                node = pending.pop()
                for predecessor in graph[node]:
                    if distances.get(predecessor) == distances[node] - 1:
                        witnesses[predecessor, node] += count
                        if predecessor not in visited:
                            visited.add(predecessor)
                            pending.append(predecessor)
    for client in manifest.clients():
        degree = len(peers[client.name])
        witnesses[client.name, client.attached_switch] = degree
        witnesses[client.attached_switch, client.name] = degree
    links = []
    for link in manifest.links:
        for a, b in (link.endpoints, link.endpoints[::-1]):
            links.append(
                LinkCoverage(
                    source=a.device,
                    destination=b.device,
                    source_interface=a.interface,
                    destination_interface=b.interface,
                    witnesses=witnesses[a.device, b.device],
                )
            )
    covered = {endpoint for link in links if link.witnesses for endpoint in (link.source, link.destination)}
    return CandidateCoverage(
        directed_links=links,
        covered_switches=[device.name for device in manifest.switches() if device.name in covered],
        minimum_witnesses=min((link.witnesses for link in links), default=0),
    )
