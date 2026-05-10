#!/usr/bin/env python3
"""Run a suite of scenarios with automatic provisioning and teardown.

The SDK provisions a runtime, runs every scenario in order, and tears
down when finished.

Usage::

    PYTHONPATH=. python examples/02_run_suite.py
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
    scenarios = require_scenarios(
        repo,
        [
            "scenarios/generated/xs/generated_link_down_xs_001.yaml",
            "scenarios/generated/xs/generated_packet_loss_xs_001.yaml",
            "scenarios/generated/xs/generated_high_latency_xs_001.yaml",
        ],
        prepare_hint=(
            "Generate benchmark assets first with "
            "`netopsbench benchmark prepare --scales xs` from the repo root."
        ),
    )

    with bench_cls(workspace=str(repo)) as bench:
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)

        print("02 — Run suite")
        print_agent_banner("agent", vendor, raw_agent)
        print(f"  scenarios: {len(scenarios)} files")
        try:
            run = bench.sessions.run_suite(
                scenarios=scenarios,
                agent=agent,
                scale="xs",
                workers=3,
            )
            return wait_and_print_run(run, raise_on_failure=True)
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    args = build_arg_parser("Run a suite of scenarios").parse_args()
    raise SystemExit(main(repo_root=args.repo_root, vendor=args.vendor))
