---
name: netopsbench-diagnosis-playbook
description: Use this skill when diagnosing NetOpsBench DCN benchmark faults with Pingmesh, topology, routing, interface, and log evidence. It helps localize a single infrastructure device or interface, avoid timeout-prone tool loops, and normalize conclusions to benchmark-friendly fault labels such as link_down, packet_loss, high_latency, packet_corruption, mtu_mismatch, blackhole_route, and static_route_misconfig.
compatibility: Requires the NetOpsBench MCP toolset with Pingmesh, topology, BGP, route, interface, log, ping, and traceroute tools.
allowed-tools: get_pingmesh_summary get_pingmesh_hotspots get_topology get_all_bgp_status get_bgp_neighbors get_route_table get_device_interfaces get_interface_metrics get_device_logs get_device_config get_bgp_rib ping_test traceroute
---

# NetOpsBench Diagnosis Playbook

## When to use

Use this skill for NetOpsBench troubleshooting tasks where you must produce a benchmark-friendly diagnosis from live MCP tool evidence.

Use it especially when one of these risks appears:

- Pingmesh anomalies are broad and you need to reduce the search space quickly
- Pingmesh shows zero anomalies, but the network may still have a routing or forwarding fault
- Pingmesh reports `mtu_or_fragmentation_suspect` but ordinary small-packet checks still look healthy
- The evidence points to a path between multiple leaves and you must choose a single owner or stay inconclusive
- You have enough evidence for a fault, but your current fault label is free-form instead of a benchmark label
- You are about to return multiple devices or a wide interface set

## Core goals

1. Prefer one infrastructure device owner over a path-level multi-device answer.
2. Prefer one interface only when evidence is strong; otherwise omit the interface.
3. Use benchmark fault labels, not descriptive prose, for `fault_type`.
4. Keep tool exploration narrow to avoid 300s/600s dead-ends.

## Fast triage

Always begin with exactly these three tools unless the user already gave equivalent outputs:

1. `get_pingmesh_summary`
2. `get_pingmesh_hotspots`
3. `get_topology`

Classify the case into one of these modes before doing anything else:

- `latency-dominant`: latency spikes are present and packet loss is absent or minor
- `loss-dominant`: packet loss events are present
- `zero-anomaly`: total Pingmesh anomalies are zero

After triage, choose at most two candidate devices for deeper validation.

If Pingmesh explicitly reports `mtu_or_fragmentation_suspect`, treat that as a special branch even when packet loss is zero and latency is mild.

## Candidate-device rules

Use these tie-breakers in order:

1. Pick the leaf or spine that appears most often in Pingmesh hotspots.
2. Prefer a single leaf over a leaf pair when one side has stronger local evidence.
3. Prefer infrastructure devices over clients unless a client-only fault is explicit.
4. If evidence is only path-level and no owner emerges, return `inconclusive` instead of listing multiple devices.

Never return comma-separated or "A and B" device locations unless the user explicitly asks for a path summary rather than a benchmark answer.

## Evidence collection by mode

### A. Latency-dominant

Use this order:

1. `get_device_interfaces` on the top candidate leaf
2. `get_interface_metrics` on the top 2-3 interfaces most likely to carry the affected traffic
3. `get_all_bgp_status` or targeted `get_bgp_neighbors`
4. `get_device_logs`
5. `get_route_table` only if routing inconsistency is still plausible

Interpretation:

- Healthy BGP plus localized latency spikes usually means `high_latency`, not a routing fault.
- If one leaf clearly owns the hotspots and its network-facing ports show pressure, return that leaf.
- If the device is clear but the exact interface is not, omit the interface instead of naming many ports.

### B. Loss-dominant

Use this order:

1. `get_device_interfaces` on the top candidate leaf
2. `get_interface_metrics` on likely path interfaces
3. `get_all_bgp_status` or targeted `get_bgp_neighbors`
4. `get_device_logs`
5. `ping_test` or `traceroute` only for one or two decisive directed checks
6. `get_route_table` only if routing evidence is needed to distinguish route faults from data-plane faults

Interpretation:

- Healthy BGP plus localized loss usually points to `packet_loss`, `link_down`, `packet_corruption`, or `mtu_mismatch`.
- If a specific interface is down or clearly degraded, prefer `link_down`.
- If connectivity still exists but loss is the dominant symptom, prefer `packet_loss`.
- If the evidence suggests integrity or fragmentation behavior rather than simple drops, consider `packet_corruption` or `mtu_mismatch`, but only if route absence is not the better explanation.

### D. MTU / fragmentation-special branch

Use this branch whenever Pingmesh reports `mtu_or_fragmentation_suspect`, even if ordinary client-to-client ping still succeeds.

Use this order:

1. `ping_test` from an affected client with default small packets
3. `ping_test` from the same client to the same destination with `payload_size=1472` and `dont_fragment=true`
4. If needed, repeat `ping_test` with a larger DF probe such as `payload_size=2000`
5. `get_device_interfaces` on the two most likely devices on the affected path
6. `get_interface_metrics` on the most relevant path interfaces
7. `get_all_bgp_status` or targeted `get_bgp_neighbors`
8. `get_route_table` only if route absence is still plausible

Interpretation:

- Small-packet success does not clear `mtu_mismatch`.
- The strongest directed symptom is: small client ping succeeds, but large DF ping fails.
- If large DF probes fail only on paths that cross one spine or one leaf uplink, rank that device/interface higher.
- If both small and large DF probes succeed, downgrade `mtu_mismatch` confidence and keep looking for another explanation.
- If BGP and routing are healthy and large DF probes fail selectively, prefer `mtu_mismatch` over route faults.

### C. Zero-anomaly

This is the highest-priority timeout trap. Do not keep repeating Pingmesh queries.

Switch immediately to this route-centric sequence:

1. `get_route_table` on the suspected owner and one nearby peer device
2. `ping_test` for one targeted failing destination only when a client-side directed check is needed
3. `traceroute` for one targeted path if needed
4. `get_all_bgp_status` or targeted `get_bgp_neighbors`
5. `get_device_config` to inspect running configuration for route or BGP misconfigurations
6. `get_device_interfaces`
7. `get_device_logs`

Interpretation:

- Zero Pingmesh anomalies does not mean healthy routing.
- If route-table evidence points to a wrong or missing static route on one device, prefer `static_route_misconfig`.
- If traffic is dropped due to forwarding behavior but the exact static misconfiguration is not proven, prefer `blackhole_route`.
- Do not fall back to generic congestion explanations when Pingmesh is clean.

## Interface ranking

Only return an interface if you can rank one interface above the others with specific evidence.

Rank interfaces using this order:

1. Topology relevance: is the interface actually on the affected path?
2. Hard state: oper-down, admin-down, or clearly abnormal counters
3. Directionality: does hotspot direction or traceroute implicate this side?
4. Consistency: do logs and interface metrics agree?

If you cannot produce one top interface, leave `location.interface` empty.

Never return a long interface bundle such as `Ethernet0/Ethernet4/Ethernet8/...` in the final benchmark answer.

## Fault label normalization

Use only these benchmark labels in `fault_type`:

- `link_down`
- `packet_loss`
- `high_latency`
- `packet_corruption`
- `mtu_mismatch`
- `blackhole_route`
- `static_route_misconfig`

Map evidence to labels like this:

- `high_latency`: latency spikes dominate, reachability mostly remains, control plane is healthy
- `packet_loss`: drop symptoms dominate, reachability still exists, no stronger route-fault evidence
- `link_down`: one specific link or interface is down or strongly isolated as failed
- `packet_corruption`: integrity-like or corruption-like path degradation is better supported than simple loss
- `mtu_mismatch`: fragmentation/selective path failure is more plausible than route absence or link-down
- `blackhole_route`: forwarding to a destination disappears due to a route/next-hop blackhole, but static-route proof is incomplete
- `static_route_misconfig`: route-table evidence points to a wrong or missing static route on a specific device

If you are currently thinking in a free-form label like "leaf-side congestion on fabric uplinks", convert it to the closest benchmark label before returning.

## Stop conditions

Do not keep exploring once you have:

- one clear device owner and enough evidence for a benchmark label, or
- no owner after checking two devices and at most three interfaces per device

If you hit the second condition, return `inconclusive` with concise evidence instead of opening more branches.

## Final answer checklist

Before returning, verify all of these:

- `verdict` is not `network_healthy` when Pingmesh anomalies exist or directed checks fail
- `fault_type` is one of the seven benchmark labels above
- `location.device` is a single device string when you have a justified owner
- `location.interface` is either one interface or empty
- `evidence` cites concrete tool findings, not speculation

## Failure modes to avoid

- Re-running Pingmesh tools after a `zero-anomaly` classification
- Treating ordinary small-packet ping success as proof that `mtu_mismatch` is absent
- Using leaf/spine-originated `ping_test` or `traceroute` as decisive evidence; prefer client-source checks
- Returning multiple devices because the path looks symmetric
- Returning huge interface sets instead of a ranked top choice
- Using descriptive prose as `fault_type`
- Spending the budget on broad scans before checking routing in zero-anomaly cases
