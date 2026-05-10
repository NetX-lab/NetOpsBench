#!/usr/bin/env python3
"""Run a BGP scenario with the optional LLM-as-judge fault type evaluator.

Same structure as ``01_run_scenario.py`` but enables the LLM judge so that
free-form agent answers like ``"BGP peer AS mismatch"`` are semantically
matched to the canonical ``bgp_neighbor_misconfig`` taxonomy instead of
failing a deterministic string compare.

The judge is activated entirely via environment variables — no code change
is needed::

    export NETOPSBENCH_FAULT_TYPE_JUDGE_ENABLED=1
    export NETOPSBENCH_FAULT_TYPE_JUDGE_API_KEY=sk-...
    # Optional: NETOPSBENCH_FAULT_TYPE_JUDGE_MODEL, _BASE_URL

See ``examples/README.md`` for the full env-var reference and the
``fault_type_judgment`` field in the report (``mode = llm_judge`` vs
``deterministic``).
"""

from __future__ import annotations

import os
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

# BGP neighbor misconfig is the scenario where the LLM judge adds the most value:
# agents often describe the fault as "BGP peer AS mismatch" or "BGP session down"
# which don't match the canonical "bgp_neighbor_misconfig" deterministically.
_SCENARIO_REL = "scenarios/generated/xs/generated_bgp_neighbor_misconfig_xs_001.yaml"


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
        _SCENARIO_REL,
        prepare_hint=(
            "Generate benchmark assets first with "
            "`netopsbench benchmark prepare --scales xs` from the repo root."
        ),
    )[0]

    judge_enabled = os.environ.get("NETOPSBENCH_FAULT_TYPE_JUDGE_ENABLED", "0") in {"1", "true", "yes"}
    judge_model = os.environ.get("NETOPSBENCH_FAULT_TYPE_JUDGE_MODEL", "gpt-4o-mini")

    print("06 — Run BGP scenario with LLM-as-judge fault type evaluator")
    print(f"  scenario  : {scenario.name}")
    print(f"  llm judge : {'enabled  (model=' + judge_model + ')' if judge_enabled else 'disabled (deterministic)'}")

    with bench_cls(workspace=str(repo)) as bench:
        raw_agent, agent = build_wrapped_agent(bench, agent_cls, vendor=vendor)
        print_agent_banner("agent     ", vendor, raw_agent)
        try:
            run = bench.sessions.run_scenario(scenario=scenario, agent=agent)
            return wait_and_print_run(run, raise_on_failure=True)
        except Exception as exc:  # noqa: BLE001 — example script
            print(f"Failed: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    args = build_arg_parser(
        "Run a BGP scenario with optional LLM-as-judge fault type evaluation"
    ).parse_args()
    raise SystemExit(main(repo_root=args.repo_root, vendor=args.vendor))
