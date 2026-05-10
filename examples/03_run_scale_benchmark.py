#!/usr/bin/env python3
"""Run every generated benchmark scenario for one topology scale.

Discovers all ``scenarios/generated/<scale>/*.yaml`` files and runs them
as a single suite with automatic provisioning and teardown.

Usage::

    PYTHONPATH=. python examples/03_run_scale_benchmark.py
    PYTHONPATH=. python examples/03_run_scale_benchmark.py --scale small
    PYTHONPATH=. python examples/03_run_scale_benchmark.py --vendor kimi
    PYTHONPATH=. python examples/03_run_scale_benchmark.py --vendor zhipu
"""

from __future__ import annotations

from pathlib import Path

from examples._common import (
    build_arg_parser,
    build_wrapped_agent,
    discover_generated_scenarios,
    print_agent_banner,
    resolve_repo_root,
    wait_and_print_run,
)
from examples.agents import MinimalDeepAgent
from netopsbench.sdk import NetOpsBench

DEFAULT_SCALE = "xs"
SCALE_CHOICES = ["xs", "small", "medium", "large"]


def main(
    repo_root: Path | None = None,
    *,
    scale: str = DEFAULT_SCALE,
    vendor: str = "minimax",
    workers: int = 3,
    bench_cls=NetOpsBench,
    agent_cls=MinimalDeepAgent,
) -> int:
    repo = resolve_repo_root(repo_root)
    scenarios = discover_generated_scenarios(repo, scale)

    with bench_cls(workspace=str(repo)) as bench:
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)

        print(f"03 — Scale benchmark (scale={scale})")
        print_agent_banner("agent", vendor, raw_agent)
        print(f"  scenarios: {len(scenarios)} files")
        for path in scenarios[:5]:
            print(f"    {path.name}")
        if len(scenarios) > 5:
            print(f"    ... and {len(scenarios) - 5} more")
        try:
            run = bench.sessions.run_suite(
                scenarios=scenarios,
                agent=agent,
                scale=scale,
                workers=workers,
            )
            return wait_and_print_run(run, raise_on_failure=True)
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    parser = build_arg_parser("Run all benchmarks for a topology scale")
    parser.add_argument("--scale", default=DEFAULT_SCALE, choices=SCALE_CHOICES)
    parser.add_argument("--workers", type=int, default=3, help="Number of concurrent worker labs")
    args = parser.parse_args()
    raise SystemExit(main(repo_root=args.repo_root, scale=args.scale, vendor=args.vendor, workers=args.workers))
