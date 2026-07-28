# NetOpsBench Test Scenarios

This directory contains the benchmark scenario corpus for NetOpsBench.

The canonical campaign spec is packaged under
`netopsbench/platform/scenario/specs/`. The `scenarios/generated/` tree is a
generated artifact and is absent on a fresh clone until you create it locally.

All scenarios are generated from campaign specs and organized under `scenarios/generated/` by topology scale:

- `scenarios/generated/xs/`
- `scenarios/generated/small/`
- `scenarios/generated/medium/`
- `scenarios/generated/large/`
- `scenarios/generated/xlarge/`
- `scenarios/generated/fat-tree-k8/`
- `scenarios/generated/fat-tree-k12/`

Pass `--spec` to use a custom campaign instead of the packaged default.

## Supported Fault Types

| Fault Type | Difficulty | Description |
|------------|-----------|-------------|
| `link_down` | Easy | Interface failure — most common DCN failure |
| `blackhole_route` | Medium | Silent packet drop from routing misconfiguration |
| `mtu_mismatch` | Hard | Intermittent connectivity based on packet size |
| `high_latency` | Medium | Performance degradation without packet loss |
| `packet_loss` | Medium | Partial packet drops on a link |
| `static_route_misconfig` | Medium | Wrong next-hop routing |
| `packet_corruption` | Hard | Data corruption on a link |
| `acl_misconfig` | Hard | Access-list rule blocks legitimate traffic |
| `bgp_neighbor_misconfig` | Hard | BGP session fails due to misconfigured peer |
| `device_down` | Hard | Entire switch becomes unreachable |
| `healthy_network` | Easy | Negative sample — no fault injected |
| `link_flapping` | Hard | Repeated interface state transitions |
| `route_policy_misconfig` | Hard | Route policy filters or redirects traffic incorrectly |

---

## Fresh-clone quickstart

Generate the XS topology metadata and scenario files before using the example
paths referenced below:

```bash
netopsbench benchmark prepare --scales xs
```

## Running Scenarios

Benchmark execution is **library-first**: use the Python SDK (`netopsbench.sdk.NetOpsBench`) or the
reference scripts under `examples/`. The `netopsbench` CLI only supports
**listing and validating** scenario files.

### Deploy the lab first

From the repo root (see `docs/content/docs/operations/deployment.mdx`):

```bash
bash scripts/runtime/deploy.sh xs lab-topology
export NETOPSBENCH_TOPOLOGY_DIR="$PWD/lab-topology/generated_topology_xs"
```

Wait for the stack to stabilize (~30s) before injecting faults or running SDK sessions.

### List scenario files (CLI)

```bash
netopsbench scenario list scenarios/generated/xs
```

Paths are resolved relative to `--workspace` (default: current directory).

### Validate a scenario (CLI)

```bash
netopsbench scenario validate scenarios/generated/xs/generated_link_down_xs_001.yaml
```

### Run scenarios (SDK / examples)

- Single scenario: `examples/01_run_scenario.py`
- Suite (parallel workers): `examples/02_run_suite.py`
- Scale benchmark: `examples/03_run_scale_benchmark.py`
- Full-stack XS smoke (optional, opt-in env): `tests/test_runtime_xs_smoke_real.py`

Example (conceptual):

```python
from netopsbench.sdk import NetOpsBench

bench = NetOpsBench(workspace=".")
run = bench.sessions.run_scenario(
    scenario="scenarios/generated/xs/generated_link_down_xs_001.yaml",
    agent=your_agent,
)
report = run.wait()
```

## Scenario File Format

```yaml
scenario_id: unique_id
name: "Human Readable Name"
description: |
  Detailed description of what this scenario tests

topology_scale: xs  # xs, small, medium, large, xlarge, fat-tree-k8, fat-tree-k12
traffic_profile: standard  # canonical background traffic model

metadata:
  difficulty: easy  # easy, medium, hard
  expected_symptoms:
    - Symptom 1
    - Symptom 2
  expected_diagnosis: fault_type
  expected_location: device:interface

episode:
  episode_id: ep001
  description: "What this episode does"
  fault_type: link_down  # or blackhole_route, mtu_mismatch, etc.
  target_device: spine1  # optional for fault_type=none
  target_interface: Ethernet0  # if applicable
  duration_seconds: 60
  stabilization_time: 15
  metadata:
    difficulty: medium
    expected_symptoms:
      - packet_loss
  parameters:
    custom_fault_parameters: values
```

## Episode Types

### Fault Types
- `none` - No fault (baseline)
- `link_down` - Interface failure
- `blackhole_route` - Route to null
- `mtu_mismatch` - MTU misconfiguration
- `high_latency` - Latency injection
- `static_route_misconfig` - Wrong next-hop

### Episode Flow
1. **Baseline** (optional) - Establish normal behavior
2. **Fault Injection** - Inject specific fault
3. **Observation** - Monitor for duration
4. **Recovery** - Restore to normal
5. **Verification** (optional) - Verify recovery

## Traffic Profile

All benchmark scenarios use the canonical `standard` profile: at most four
continuous flows per client, split evenly between UDP and TCP. Keeping one
background traffic model avoids changing load as an untracked variable between
fault types.

## PPS Limits by Topology

The canonical switch PPS limit is **5000**. Per-client PPS caps are part of the
scale profile so benchmark load cannot change through an undocumented process
environment override.
Per-client PPS caps scale linearly from the 1000 PPS baseline:

| Scale | Clients | Base max PPS/client (1000 PPS switch limit) |
|---|---:|---:|
| xs | 2 | 250 |
| small | 8 | 250 |
| medium | 16 | 200 |
| large | 64 | 150 |
| xlarge | 128 | 100 |
| fat-tree-k8 | 128 | 100 |
| fat-tree-k12 | 144 | 50 |

Example (5000 PPS limit):
- xs/small: 1250 PPS per client
- medium: 1000 PPS per client
- large: 750 PPS per client
- xlarge/fat-tree-k8: 500 PPS per client
- fat-tree-k12: 250 PPS per client

## Results

Scenario results are saved to `.netopsbench/runs/` directory:
- JSON format
- Timestamped
- Contains full execution trace
- Includes observations and recovery status

## Creating Custom Scenarios

### Option A: Generate in bulk from spec (recommended)

```bash
# Generate reproducible randomized scenarios for one scale
python3 -m netopsbench.platform.scenario.generator --scale xs --seed 42

# Output defaults to scenarios/generated/xs — validate then run via SDK (see above)
netopsbench scenario validate scenarios/generated/xs/<file>.yaml
```

Pass `--spec` if your campaign spec is not the default (see `python3 -m netopsbench.platform.scenario.generator --help`).

`scenarios/generated/` is a generated artifact tree, not the hand-written
benchmark corpus. It can be deleted and regenerated from spec files.

### Option B: Hand-write a single scenario

1. Copy an existing scenario as template
2. Modify scenario_id, name, description
3. Adjust the episode for your test case
4. Validate: `netopsbench scenario validate your_scenario.yaml`
5. Run through the SDK (`NetOpsBench.sessions.run_scenario` / `run_suite`) or an `examples/` script

## Best Practices

1. **Always start with baseline episode** - Establishes normal behavior
2. **Include recovery verification** - Ensures clean state
3. **Use appropriate stabilization times** - Allow faults to manifest
4. **Match traffic to topology** - Don't exceed PPS limits
5. **Document expected behavior** - In metadata for evaluation
