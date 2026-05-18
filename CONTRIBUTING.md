# Contributing to NetOpsBench

Thanks for helping improve NetOpsBench. This project is an early-stage DCN troubleshooting benchmark, so focused changes with clear behavior, tests, and documentation are especially valuable.

## Development Setup

```bash
git clone https://github.com/NetX-lab/NetOpsBench.git
cd NetOpsBench
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,agent]"
```

Real lab runs also require Docker, Containerlab, GNU Parallel, and non-interactive access to the required Docker and Containerlab commands. For the runtime path, start from [docs/content/docs/operations/deployment.mdx](docs/content/docs/operations/deployment.mdx).

## Docs Site

The documentation website is now maintained from [docs](docs). To preview it locally:

```bash
cd docs
npm install
npm run build
npm run start
```

`npm run start` serves the static export from `docs/out` and automatically retries on the next free port if the requested port is busy. You can still request a preferred port with `npm run start -- --port 3010`.

## Environment

Runtime secrets are loaded from environment variables or a repo-root `.env` file. Do not commit `.env`.

Common variables:

- `MINIMAX_API_KEY`, `OPENAI_API_KEY`, `ZHIPU_API_KEY`, `DEEPSEEK_API_KEY`, `KIMI_API_KEY` — provider keys for example agents
- `NETOPSBENCH_INFLUXDB_URL`, `NETOPSBENCH_INFLUXDB_TOKEN`, `NETOPSBENCH_INFLUXDB_ORG`, `NETOPSBENCH_INFLUXDB_BUCKET`
- `NETOPSBENCH_LOG_LEVEL` — logging verbosity, for example `DEBUG`
- `NETOPSBENCH_NO_SUDO=1` — suppress sudo in CI or tests

See [.env.example](.env.example) for the fuller reference.

## Public Boundaries

Prefer public APIs in new user-facing code:

- `netopsbench.sdk.*`
- `netopsbench.agents.base`
- `examples/*`
- `scripts/*`

Treat `netopsbench.platform.*` as internal unless you are intentionally changing runtime, scenario execution, observability, or fault-injection internals. The main reference pages are:

- [docs/content/docs/api/quickstart.mdx](docs/content/docs/api/quickstart.mdx)
- [docs/content/docs/architecture/system-overview.mdx](docs/content/docs/architecture/system-overview.mdx)
- [docs/content/docs/contribute/repository-layout.mdx](docs/content/docs/contribute/repository-layout.mdx)

## Testing

Run the main non-real regression suite before submitting changes:

```bash
pytest -q tests/test_fault_registry.py \
  tests/test_scenario_schema.py \
  tests/test_scenario_commands.py \
  tests/test_commands_cli.py \
  tests/test_route_parser_and_static_route.py \
  tests/test_runtime_config_consistency.py \
  tests/test_topology_fail_fast.py \
  tests/test_platform_toolkit_fastmcp.py \
  tests/test_example_agents.py \
  tests/test_agent_base_contract.py
```

Tests marked `real` require a deployed lab and external services:

```bash
pytest -m real
```

If you modify the docs site, also validate it with:

```bash
cd docs
npm run build
```

## Extension Guides

- Custom agents: [docs/content/docs/build-your-agent/custom-agents.mdx](docs/content/docs/build-your-agent/custom-agents.mdx)
- Custom faults (external/project-local): [docs/content/docs/contribute/custom-faults.mdx](docs/content/docs/contribute/custom-faults.mdx)
- Built-in fault catalog: [docs/content/docs/contribute/fault-types.mdx](docs/content/docs/contribute/fault-types.mdx)
- Scenario format: [scenarios/README.md](scenarios/README.md)

### Adding a built-in fault type (maintainers)

When adding a fault to the core distribution rather than an external pack:

1. Add inject/recover handlers under `netopsbench/platform/faults/handlers/<category>.py`.
2. Register a `FaultSpec` in the matching `netopsbench/platform/faults/builtin/<category>_specs.py` (set `requires_interface` / `requires_prefix` / `required_parameters` as needed).
3. Update the scenario parser in `netopsbench/platform/scenario/parser.py` if the fault introduces new fields.
4. Add a scenario example under `scenarios/` and tests under `tests/`.
5. Add a row to [docs/content/docs/contribute/fault-types.mdx](docs/content/docs/contribute/fault-types.mdx).

## Pull Requests

Good PRs are small, explain the behavior change, include relevant tests, and update docs when public interfaces or benchmark behavior change.

If you change package ownership, script entrypoints, public import paths, runtime defaults, or documentation structure, update the related entry points in the same PR:

- [README.md](README.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/content/docs](docs/content/docs)

If a change affects benchmark behavior, runtime defaults, or import boundaries, call that out explicitly in the PR description.
