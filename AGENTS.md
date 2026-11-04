# Repository Guide for Coding Agents

This file is for Codex, Claude Code, Cursor, Aider, and other agentic
programming tools that modify this repository. It is not a tutorial for
benchmark diagnosis agents. If you want to build a troubleshooting agent that
NetOpsBench evaluates, start with
`docs/content/docs/build-your-agent/custom-agents.mdx` and
`examples/agents/README.md`.

Keep changes small, preserve benchmark semantics, verify behavior with focused
tests, and avoid changing the public SDK/API surface unless the task explicitly
asks for a breaking change.

## Project Shape

- `netopsbench/sdk/`: stable public Python SDK. User-facing sessions,
  runtimes, agents, MCP config, reports, scenario helpers, faults, and
  evaluators live here.
- `netopsbench/platform/`: internal runtime implementation. This includes
  topology/runtime orchestration, scenario execution, worker pools, faults,
  traffic, Pingmesh, toolkit, and observability internals. Changes here should
  include focused tests.
- `netopsbench/platform/toolkit/`: direct toolkit methods and FastMCP wrappers
  exposed to troubleshooting agents.
- `netopsbench/platform/scenario/`: scenario parsing, models, validation,
  execution, observation collection, and episode handling.
- `netopsbench/platform/session/`: session orchestration, runtime dispatch,
  diagnosis callbacks, scoring handoff, and reporting.
- `examples/`: public runnable examples. Treat these as part of the user
  experience; keep commands, docs, and imports aligned.
- `scenarios/generated/<scale>/`: generated benchmark scenarios used by
  examples and suite runs.
- `netopsbench/platform/observability/assets/`: packaged Grafana, Telegraf,
  and Docker Compose assets.
- `scripts/runtime/`: documented thin wrappers around the Python runtime CLI.
- `tests/`: lightweight unit and contract tests. Real Containerlab tests are
  usually marked `real` or named `*_real`.

The stable external boundary is `netopsbench.sdk`, the documented CLI commands,
the docs under `docs/`, and public examples under `examples/`. Treat
`netopsbench.platform.*` as internal unless a task explicitly asks to expose or
document it.

## Benchmark Runtime Flow

The normal path is:

1. A user calls an example, CLI command, or SDK session API.
2. The session layer provisions or attaches to a runtime worker pool.
3. The scenario executor runs episodes and manages traffic/fault lifecycle.
4. Baseline and fault-window observations are collected.
5. A troubleshooting agent receives `DiagnosticContext` and returns
   `DiagnosisResult`.
6. The evaluator scores detection, localization, fault type, runtime, tool
   usage, and token usage.
7. Reports and raw artifacts are written for the run.

Do not confuse coding agents editing this repository with troubleshooting
agents evaluated by the benchmark. Troubleshooting agents implement
`diagnose(context) -> DiagnosisResult`; coding agents should preserve that
contract while modifying the codebase.

Important files for this path:

- `netopsbench/platform/session/orchestrator.py`
- `netopsbench/platform/session/dispatch.py`
- `netopsbench/platform/scenario/executor.py`
- `netopsbench/platform/scenario/episode_runner.py`
- `netopsbench/platform/session/scoring.py`
- `netopsbench/evaluator/scorer.py`

## Benchmark Invariants

### Pingmesh Query Windows

Pingmesh tools must query the episode window, not an arbitrary rolling window,
when scenario diagnosis is running. Preserve this precedence:

1. Explicit tool args: `start_time` and `end_time`
2. The current `AgentToolkit` default Pingmesh window
3. `NETOPSBENCH_PINGMESH_CONTEXT_FILE`, a JSON file with
   `{"start_time": "...Z", "end_time": "...Z"}`
4. `NETOPSBENCH_PINGMESH_START_TIME` and `NETOPSBENCH_PINGMESH_END_TIME`
5. Rolling fallback via `time_range_minutes`

Do not remove or reorder this behavior without updating tests and docs.
Relevant files:

- `netopsbench/platform/toolkit/_core/observability/pingmesh_scope.py`
- `netopsbench/platform/toolkit/_core/observability/pingmesh_ops.py`
- `netopsbench/platform/toolkit/mcp/observability.py`
- `netopsbench/platform/session/orchestrator.py`
- `tests/test_pingmesh_time_scope.py`

`get_pingmesh_hotspots` is intentionally leaf-pair aggregated. Do not change it
to client-path granularity unless the benchmark design changes.

### Negative Samples

Healthy scenarios are represented by scenario metadata:

```yaml
metadata:
  negative_sample: true
```

Each scenario has one canonical `episode`. A negative sample uses
`fault_type: none`: it collects a complete healthy observation window and
calls the agent. Do not skip healthy scenarios or restore legacy multi-episode
selection logic.

Scoring intent:

- Positive cases: main score is localization-oriented; fault type is a separate
  KPI.
- Negative cases: score is 1 only when the verdict is `network_healthy`;
  `fault_detected` and `inconclusive` score 0.
- Detection summaries include positive and negative cases.
- Localization and fault-type summaries aggregate only positive cases.

Relevant tests:

- `tests/test_negative_sample_scenarios.py`
- `tests/test_scenario_schema.py`
- `tests/test_e2e.py`

### Runtime Observability

The runtime currently relies on host-side helper scripts for Telegraf,
Pingmesh, and BGP snapshots. Preserve these behaviors:

- BGP line protocol includes `topology_id`.
- Telegraf tails `/var/lib/netopsbench/bgp_neighbors.lp` from the beginning and
  uses `watch_method = "poll"`.
- Worker Telegraf config and BGP line protocol files are readable by the
  Telegraf container.

Relevant files:

- `netopsbench/platform/observability/lifecycle.py`
- `netopsbench/platform/observability/bgp_collector.py`
- `netopsbench/platform/observability/assets/telegraf.conf.template`
- `tests/test_bgp_collector.py`
- `tests/test_runtime_config_consistency.py`

## Coding Agent Guidelines

- Prefer small, targeted changes over broad rewrites.
- Preserve public SDK imports, public example behavior, and documented CLI
  commands unless the task explicitly asks for a breaking change.
- Keep `examples/`, docs, and tests aligned. If a command or public API changes,
  update the example and the relevant docs in the same change.
- Do not treat generated runtime artifacts as source. Do not commit caches,
  `.env` files, `.pytest_cache/`, `__pycache__/`, `scenario_results/`,
  `lab-topology/`, `.netopsbench*/`, or local virtual environments.
- Do not run real Containerlab tests by default. Run them only when the user asks
  or when the environment is confirmed to support Docker, Containerlab, and
  non-interactive privileged commands.
- If you touch `netopsbench/platform/*`, add or update focused tests for the
  behavior. If you touch `netopsbench/sdk/*`, preserve the public API contract
  or document the migration.
- Use structured parsers and existing helpers instead of ad hoc string handling
  when the codebase already provides them.

## Useful Commands

Lightweight tests for Pingmesh, negative samples, SDK contracts, scenarios, and
examples:

```bash
python -m pytest \
  tests/test_pingmesh_time_scope.py \
  tests/test_negative_sample_scenarios.py \
  tests/test_runtime_agent_input_contract.py \
  tests/test_api_sessions.py \
  tests/test_scenario_schema.py \
  tests/test_e2e.py \
  tests/test_scenario_commands.py \
  tests/test_example_agents.py
```

If you use a local virtual environment, replace `python` with that interpreter.

Run one public example from the repository root:

```bash
netopsbench benchmark prepare --scales xs
export OPENAI_API_KEY=...
PYTHONPATH=. python examples/01_run_scenario.py --vendor openai
```

Run the XS benchmark suite with a provider of your choice:

```bash
BENCH_VENDOR=openai BENCH_SCALES=xs bash scripts/run_all_benchmarks.sh
```

Before committing or handing off changes:

```bash
git diff --check
git status --short --branch
```

## Git Hygiene

- Do not revert user changes unless explicitly requested.
- Treat untracked files as user-owned unless the task clearly asks to add them.
- Avoid merging or checking out feature branches unless the user asks for that
  branch.
- Keep public examples and SDK docs aligned with code changes.
