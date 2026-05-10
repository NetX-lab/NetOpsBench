---
name: network_diagnosis
description: Example-local troubleshooting guidance for the MinimalDeepAgent DeepAgents demo.
allowed-tools: get_pingmesh_summary get_pingmesh_hotspots get_topology get_device_interfaces get_interface_metrics get_bgp_neighbors get_all_bgp_status get_route_table get_device_logs ping_test traceroute
---

# Network Diagnosis Skill

Use this skill for the public DeepAgent example when you need to diagnose a likely fabric fault from MCP evidence.

## Preferred flow

1. Start with `get_pingmesh_summary`, `get_pingmesh_hotspots`, and `get_topology`.
2. Pick one likely owner device when possible.
3. Validate with the smallest useful set of device-level checks:
   - `get_device_interfaces`
   - `get_interface_metrics`
   - `get_bgp_neighbors` or `get_all_bgp_status`
   - `get_route_table`
   - `get_device_logs`
4. Use `ping_test` and `traceroute` only when they are likely to clarify the fault.

## Output guidance

- Prefer a single device and at most one interface.
- Return benchmark-style fault labels when the evidence is clear.
- If the evidence is weak, return `inconclusive` instead of guessing.
- Keep the final reasoning short and tied to tool output.
