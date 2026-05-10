#!/usr/bin/env python3
"""Register a custom fault pack, then run a scenario that uses it.

Demonstrates the extension flow:

1. Build a ``FaultPack`` from ``examples/faults/custom_fault_pack``.
2. Register it with ``bench.faults.register_pack(...)``.
3. Run a scenario that references the custom ``demo_custom_latency`` fault.

Usage::

    PYTHONPATH=. python examples/04_custom_faults.py
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
from examples.faults.custom_fault_pack import build_fault_pack
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
        "examples/faults/custom_fault_pack/scenario.yaml",
        prepare_hint="The custom fault example scenario should be present in the repository checkout.",
    )[0]

    print("04 — Custom faults")
    print(f"  scenario: {scenario.name}")
    print(f"  custom fault: demo_custom_latency")
    with bench_cls(workspace=str(repo)) as bench:
        bench.faults.register_pack(build_fault_pack())
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)
        print_agent_banner("agent", vendor, raw_agent)
        try:
            run = bench.sessions.run_scenario(scenario=scenario, agent=agent)
            return wait_and_print_run(run, raise_on_failure=True)
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    args = build_arg_parser("Register and run a custom fault scenario").parse_args()
    raise SystemExit(main(repo_root=args.repo_root, vendor=args.vendor))
