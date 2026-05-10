#!/usr/bin/env python3
"""Run a single scenario with automatic provisioning and teardown.

This is the simplest way to use NetOpsBench.  The SDK provisions a
Containerlab runtime, runs the scenario, and tears everything down
automatically.

Usage::

    PYTHONPATH=. python examples/01_run_scenario.py
    PYTHONPATH=. python examples/01_run_scenario.py --vendor openai
"""

from __future__ import annotations

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

    print("01 — Run scenario")
    print(f"  scenario: {scenario.name}")

    # ``with`` releases agent handles + manager resources on exit.
    with bench_cls(workspace=str(repo)) as bench:
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)
        print_agent_banner("agent", vendor, raw_agent)
        try:
            run = bench.sessions.run_scenario(scenario=scenario, agent=agent)
            return wait_and_print_run(run, raise_on_failure=True)
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    args = build_arg_parser("Run one scenario").parse_args()
    raise SystemExit(main(repo_root=args.repo_root, vendor=args.vendor))
