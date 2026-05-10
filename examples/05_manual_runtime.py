#!/usr/bin/env python3
"""Provision a runtime manually and keep it alive after the run.

Useful when you want to iterate on the agent or inspect the topology
without reprovisioning each time.  The runtime is *not* torn down
automatically — call ``runtime.teardown()`` when you are done.

Usage::

    PYTHONPATH=. python examples/05_manual_runtime.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from examples._common import (
    build_arg_parser,
    build_wrapped_agent,
    print_agent_banner,
    require_scenarios,
    resolve_repo_root,
    wait_and_print_run,
)
from examples.agents import MinimalDeepAgent
from netopsbench.sdk import NetOpsBench


def main(
    repo_root: Path | None = None,
    *,
    vendor: str = "minimax",
    bench_cls=NetOpsBench,
    agent_cls=MinimalDeepAgent,
) -> int:
    repo = resolve_repo_root(repo_root)
    scenario = require_scenarios(
        repo,
        "scenarios/generated/xs/generated_link_down_xs_001.yaml",
        prepare_hint=(
            "Generate benchmark assets first with "
            "`netopsbench benchmark prepare --scales xs` from the repo root."
        ),
    )[0]

    runtime_name = f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    artifacts_dir = repo / "scenario_results" / "manual_runtime"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    print("05 — Manual runtime (no auto-teardown)")
    print(f"  scenario: {scenario.name}")
    print(f"  runtime:  {runtime_name}")
    with bench_cls(workspace=str(repo)) as bench:
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)
        print_agent_banner("agent", vendor, raw_agent)
        try:
            runtime = bench.runtimes.provision(scale="xs", workers=1, name=runtime_name)
            run = bench.sessions.run_on_runtime_scenario(
                scenario=scenario,
                runtime=runtime,
                agent=agent,
                artifacts_dir=artifacts_dir,
            )
            exit_code = wait_and_print_run(run, raise_on_failure=True)
            if exit_code != 0:
                return exit_code

            print()
            print(f"  Runtime '{runtime_name}' is still running.")
            print(f"  Tear it down later with an attached runtime handle or cleanup command.")
            return 0
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    args = build_arg_parser("Provision a runtime manually").parse_args()
    raise SystemExit(main(repo_root=args.repo_root, vendor=args.vendor))
