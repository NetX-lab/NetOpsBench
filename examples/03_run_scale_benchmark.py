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
from typing import Any

from dotenv import load_dotenv

from examples._common import (
    build_arg_parser,
    discover_generated_scenarios,
    print_agent_banner,
    resolve_repo_root,
    wait_and_print_run,
)
from examples.agents import MinimalDeepAgent
from netopsbench.sdk import NetOpsBench, supported_scales

DEFAULT_SCALE = "xs"
SCALE_CHOICES = list(supported_scales())


def _construct_agent(agent_cls: Any, *, vendor: str) -> Any:
    try:
        return agent_cls(vendor=vendor)
    except TypeError:
        return agent_cls()


def main(
    repo_root: Path | None = None,
    *,
    scale: str = DEFAULT_SCALE,
    vendor: str = "minimax",
    workers: int = 3,
    agent_mode: str = "original",
    scale_profile: Path | None = None,
    only_scenario_ids: tuple[str, ...] = (),
    bench_cls=NetOpsBench,
    agent_cls=MinimalDeepAgent,
) -> int:
    if agent_mode not in {"original", "harness"}:
        raise ValueError("agent_mode must be 'original' or 'harness'")
    repo = resolve_repo_root(repo_root)
    load_dotenv(repo / ".env", override=False)
    scenarios = discover_generated_scenarios(repo, scale)
    if only_scenario_ids:
        discovered = {path.stem: path for path in scenarios}
        unknown = set(only_scenario_ids) - set(discovered)
        if unknown:
            raise ValueError(f"unknown {scale} scenario IDs: {sorted(unknown)}")
        scenarios = [discovered[scenario_id] for scenario_id in only_scenario_ids]

    bench_kwargs: dict[str, Any] = {"workspace": str(repo)}
    if scale_profile is not None:
        bench_kwargs["scale_profiles"] = [scale_profile]
    with bench_cls(**bench_kwargs) as bench:
        raw_agent = _construct_agent(agent_cls, vendor=vendor)
        selected_agent = raw_agent
        if agent_mode == "harness":
            from examples.agents.diagnostic_harness import DiagnosticHarness
            from examples.agents.diagnostic_harness.config import FeatureConfig, HarnessConfig, TelemetryConfig

            selected_agent = DiagnosticHarness(
                raw_agent,
                config=HarnessConfig(
                    impairment_probes=FeatureConfig(enabled=True),
                    topology_ranker=FeatureConfig(enabled=True),
                    diagnosability_gate=FeatureConfig(enabled=True),
                    telemetry=TelemetryConfig(enabled=True),
                ),
            )
        wrap = getattr(getattr(bench, "agents", None), "wrap", None)
        agent = wrap(selected_agent) if callable(wrap) else selected_agent

        print(f"03 — Scale benchmark (scale={scale}, agent={agent_mode})")
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
    parser.add_argument(
        "--scale",
        default=DEFAULT_SCALE,
        choices=SCALE_CHOICES,
        help="Topology scale whose generated scenarios should be discovered and run. Default: %(default)s.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of concurrent runtime worker labs to provision. Default: %(default)s.",
    )
    parser.add_argument(
        "--agent",
        dest="agent_mode",
        choices=("original", "harness"),
        default="original",
        help="Run the original example agent or diagnostic harness. Default: %(default)s.",
    )
    parser.add_argument(
        "--scale-profile",
        type=Path,
        default=None,
        help="Optional validated scale-profile override, for example an isolated management subnet.",
    )
    parser.add_argument(
        "--only-scenarios",
        nargs="*",
        default=[],
        help="Run only these exact generated scenario IDs, preserving the supplied order.",
    )
    args = parser.parse_args()
    raise SystemExit(
        main(
            repo_root=args.repo_root,
            scale=args.scale,
            vendor=args.vendor,
            workers=args.workers,
            agent_mode=args.agent_mode,
            scale_profile=args.scale_profile,
            only_scenario_ids=tuple(args.only_scenarios),
        )
    )
