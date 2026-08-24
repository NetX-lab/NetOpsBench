---
name: network-diagnosis
description: Example-local troubleshooting guidance for the MinimalDeepAgent DeepAgents demo.
allowed-tools: get_pingmesh_summary get_pingmesh_hotspots get_topology get_device_interfaces get_interface_metrics query_bgp_events get_bgp_neighbors get_bgp_neighbor get_bgp_rib get_route_table get_device_config get_device_logs ping_test traceroute
---

# Network Diagnosis Skill

Use this skill for the public DeepAgent example when you need to diagnose a likely fabric fault from MCP evidence.

## Preferred flow

1. Start with `get_pingmesh_summary`, `get_pingmesh_hotspots`, and `get_topology`.
2. Before declaring the network healthy, call `query_bgp_events` for the episode window.
3. Pick one likely owner device when possible and validate with the smallest useful set of device-level checks:
   - `get_device_interfaces`
   - `get_interface_metrics`
   - For each event, confirm only the affected device with `get_bgp_neighbors`, then inspect the peer with `get_bgp_neighbor`.
   - Use `get_bgp_rib`, `get_route_table`, or `get_device_config` only when route impact or configuration needs confirmation.
   - Do not assume client-facing prefixes should be `/24`. NetOpsBench commonly uses `/30`
     point-to-point access subnets; compare the configured prefix with topology and route
     evidence before calling it a misconfiguration.
   - Report the canonical fault label `bgp_neighbor_misconfig`; do not substitute `bgp_down` or `link_flap`.
   - For an AS mismatch, set `location.device` to the device whose configured remote-AS disagrees with the peer's actual local AS. Do not report the correctly configured peer.
   - Separate a control-plane session event from confirmed data-plane impact. Historical events outside the episode window are not current faults.
   - `get_device_logs`: always pass the observation `start_time` and `end_time`; ignore log entries outside that episode window.
4. Use `ping_test` and `traceroute` only when they are likely to clarify the fault. `traceroute`
   must originate from a client; use `ping_test` for probes sourced from switches.
   Do not use jumbo DF probes unless topology evidence says that payload size is supported;
   failure above the configured path MTU is expected and does not prove an MTU mismatch.

When low-rate Pingmesh anomalies are not persistent, current probes succeed, interfaces are
healthy, routes are installed, and BGP is established, return `network_healthy`. Do not keep
expanding the search solely to explain isolated loss or jitter samples.

Treat Pingmesh `src_leaf`/`dst_leaf` as legacy names for client attachment switches; they may be CLOS leaves or
fat-tree edges. For a symmetric attachment pair, do not infer the faulty side from probe direction alone: an echoed
reply can cross a discard route in either direction. Inspect the exact affected client IP on both attachment switches with
`get_route_table`; prefer structured `is_discard`/`discard_interface` evidence over interpreting the route legend.
When the selected route is a discard/Null0 route, report the canonical label `blackhole_route`. Reserve
`static_route_misconfig` for a non-discard static route whose selected next hop or egress path is incorrect.
An exact client route is sufficient to close a route diagnosis when one endpoint selects that static route, the
other endpoint has no matching static override, and BGP remains established. Return the diagnosis at that point;
do not fan out into transit switches, broad route-table dumps, logs, ACLs, or traceroute unless the exact route
evidence is missing or contradictory.

## Output guidance

- Prefer a single device and at most one interface.
- Return benchmark-style fault labels when the evidence is clear.
- If the evidence is weak, return `inconclusive` instead of guessing.
- Keep the final reasoning short and tied to tool output.
