#!/usr/bin/env python3
"""NetOpsBench CLI entrypoint (SDK-first, generation-capable)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netopsbench.logging_utils import configure_logging
from netopsbench.platform.scenario import generator as scenario_generator
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.sdk import NetOpsBench

SUPPORTED_SCALES = ("xs", "small", "medium", "large")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NetOpsBench CLI")
    parser.add_argument("--workspace", default=".", help="Workspace directory for runtime metadata")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show SDK-visible platform status")

    runtime_parser = subparsers.add_parser("runtime", help="Runtime lifecycle operations")
    runtime_sub = runtime_parser.add_subparsers(dest="runtime_action", required=True)
    runtime_sub.add_parser("list", help="List known runtimes")
    runtime_show = runtime_sub.add_parser("show", help="Show one runtime details")
    runtime_show.add_argument("name", help="Runtime name")
    runtime_teardown = runtime_sub.add_parser("teardown", help="Tear down a runtime")
    teardown_target = runtime_teardown.add_mutually_exclusive_group(required=True)
    teardown_target.add_argument("name", nargs="?", default=None, help="Runtime name")
    teardown_target.add_argument("--all", dest="teardown_all", action="store_true", help="Tear down all runtimes")

    topology_parser = subparsers.add_parser("topology", help="Topology generation operations")
    topology_sub = topology_parser.add_subparsers(dest="topology_action", required=True)
    topology_generate = topology_sub.add_parser("generate", help="Generate topology metadata for one scale")
    topology_generate.add_argument("--scale", required=True, choices=SUPPORTED_SCALES, help="Topology scale")
    topology_generate.add_argument("--out", help="Output directory (default: lab-topology/generated_topology_<scale>)")

    scenario_parser = subparsers.add_parser("scenario", help="Scenario query and generation operations")
    scenario_sub = scenario_parser.add_subparsers(dest="scenario_action", required=True)
    scenario_list = scenario_sub.add_parser("list", help="List scenario files")
    scenario_list.add_argument("path", nargs="?", default="scenarios", help="Scenario file or directory")
    scenario_validate = scenario_sub.add_parser("validate", help="Validate one scenario YAML")
    scenario_validate.add_argument("file", help="Scenario YAML file")
    scenario_generate = scenario_sub.add_parser("generate", help="Generate scenario YAML for one scale")
    scenario_generate.add_argument("--scale", required=True, choices=SUPPORTED_SCALES, help="Topology scale")
    scenario_generate.add_argument(
        "--spec", help="Scenario generation spec file (default: scenarios/specs/fault_campaign.yaml)"
    )
    scenario_generate.add_argument(
        "--topology-dir", help="Generated topology directory (default: lab-topology/generated_topology_<scale>)"
    )
    scenario_generate.add_argument("--out", help="Output directory (default: scenarios/generated/<scale>)")
    scenario_generate.add_argument("--seed", type=int, default=42, help="Random seed for reproducible generation")

    result_parser = subparsers.add_parser("result", help="Inspect benchmark results")
    result_sub = result_parser.add_subparsers(dest="result_action", required=True)
    result_list = result_sub.add_parser("list", help="List result reports")
    result_list.add_argument("--dir", default="scenario_results", help="Results directory (default: scenario_results)")
    result_show = result_sub.add_parser("show", help="Show a result report")
    result_show.add_argument("path", help="Path to report.json")

    benchmark_parser = subparsers.add_parser("benchmark", help="Benchmark preparation helpers")
    benchmark_sub = benchmark_parser.add_subparsers(dest="benchmark_action", required=True)
    benchmark_prepare = benchmark_sub.add_parser(
        "prepare", help="Generate topology and scenarios for one or more scales"
    )
    benchmark_prepare.add_argument(
        "--scales",
        default=",".join(SUPPORTED_SCALES),
        help="Comma-separated scale list (default: xs,small,medium,large)",
    )
    benchmark_prepare.add_argument(
        "--spec", help="Scenario generation spec file (default: scenarios/specs/fault_campaign.yaml)"
    )
    benchmark_prepare.add_argument("--seed", type=int, default=42, help="Random seed for reproducible generation")

    return parser


def _cmd_status(bench: NetOpsBench) -> int:
    runtimes = bench.runtimes.list()
    print("NetOpsBench Status")
    print(f"workspace: {bench.workspace}")
    print(f"runtimes: {len(runtimes)}")
    return 0


def _cmd_runtime(bench: NetOpsBench, args: argparse.Namespace) -> int:
    if args.runtime_action == "list":
        runtimes = bench.runtimes.list()
        if not runtimes:
            print("no runtimes")
            return 0
        rows = [(r.name, r.scale, r.state, str(r.size)) for r in runtimes]
        headers = ("NAME", "SCALE", "STATE", "WORKERS")
        widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        for row in rows:
            print(fmt.format(*row))
        return 0
    if args.runtime_action == "show":
        runtime = bench.runtimes.get(args.name)
        if runtime is None:
            print(f"runtime not found: {args.name}")
            return 1
        print(json.dumps(runtime._payload(), indent=2))
        return 0
    if args.runtime_action == "teardown":
        if args.teardown_all:
            runtimes = bench.runtimes.list()
            if not runtimes:
                print("no runtimes to tear down")
                return 0
            for runtime in runtimes:
                runtime.teardown()
                print(f"torn down: {runtime.name}")
            print(f"torn down {len(runtimes)} runtime(s)")
            return 0
        runtime = bench.runtimes.get(args.name)
        if runtime is None:
            print(f"runtime not found: {args.name}")
            return 1
        runtime.teardown()
        print(f"torn down: {runtime.name}")
        return 0
    raise AssertionError(f"unhandled runtime action: {args.runtime_action}")


def _default_scenario_spec(workspace: Path) -> Path:
    return workspace / "scenarios" / "specs" / "fault_campaign.yaml"


def _default_topology_output_dir(workspace: Path, scale: str) -> Path:
    return workspace / "lab-topology" / f"generated_topology_{scale}"


def _default_scenario_output_dir(workspace: Path, scale: str) -> Path:
    return workspace / "scenarios" / "generated" / scale


def _generate_topology(workspace: Path, *, scale: str, output_dir: Path | None = None) -> int:
    destination = Path(output_dir) if output_dir is not None else _default_topology_output_dir(workspace, scale)
    result = generate_topology(scale=scale, output_dir=str(destination))
    print(f"generated topology: scale={scale}")
    print(f"  output_dir: {destination}")
    metadata_file = result.get("metadata_file") if isinstance(result, dict) else None
    if metadata_file:
        print(f"  metadata_file: {metadata_file}")
    return 0


def _generate_scenarios(
    workspace: Path,
    *,
    scale: str,
    spec: Path,
    topology_dir: Path | None = None,
    out: Path | None = None,
    seed: int = 42,
) -> int:
    spec_path = Path(spec)
    topo_dir = Path(topology_dir) if topology_dir is not None else _default_topology_output_dir(workspace, scale)
    out_dir = Path(out) if out is not None else _default_scenario_output_dir(workspace, scale)
    loaded_spec = scenario_generator.load_yaml(spec_path)
    topology = scenario_generator.load_topology(scale, str(topo_dir))
    removed = scenario_generator.cleanup_existing_outputs(out_dir)
    if removed:
        print(f"removed existing scenarios: {removed}")
    generated = scenario_generator.generate(loaded_spec, topology, out_dir, seed)
    print(f"generated scenarios: scale={scale}")
    print(f"  spec: {spec_path}")
    print(f"  topology_dir: {topo_dir}")
    print(f"  output_dir: {out_dir}")
    print(f"  count: {len(generated)}")
    return 0


def _parse_scales(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    invalid = [item for item in values if item not in SUPPORTED_SCALES]
    if invalid:
        raise ValueError(f"unsupported scales: {', '.join(invalid)}")
    if not values:
        raise ValueError("at least one scale is required")
    return values


def _cmd_topology(bench: NetOpsBench, args: argparse.Namespace) -> int:
    if args.topology_action == "generate":
        out = Path(args.out) if args.out else None
        return _generate_topology(bench.workspace, scale=args.scale, output_dir=out)
    raise AssertionError(f"unhandled topology action: {args.topology_action}")


def _cmd_scenario(bench: NetOpsBench, args: argparse.Namespace) -> int:
    if args.scenario_action == "list":
        raw_target = Path(args.path)
        target = raw_target if raw_target.is_absolute() else (bench.workspace / raw_target)
        if target.is_file():
            print(target)
            return 0
        for path in sorted(target.glob("*.y*ml")):
            print(path)
        return 0
    if args.scenario_action == "validate":
        raw_file = Path(args.file)
        scenario_file = raw_file if raw_file.is_absolute() else (bench.workspace / raw_file)
        try:
            bench.scenarios.load(scenario_file)
        except Exception as exc:
            print(f"invalid: {scenario_file} ({exc})")
            return 1
        print(f"valid: {scenario_file}")
        return 0
    if args.scenario_action == "generate":
        spec = Path(args.spec) if args.spec else _default_scenario_spec(bench.workspace)
        topology_dir = Path(args.topology_dir) if args.topology_dir else None
        out = Path(args.out) if args.out else None
        return _generate_scenarios(
            bench.workspace, scale=args.scale, spec=spec, topology_dir=topology_dir, out=out, seed=args.seed
        )
    raise AssertionError(f"unhandled scenario action: {args.scenario_action}")


def _cmd_result(bench: NetOpsBench, args: argparse.Namespace) -> int:
    from netopsbench.sdk.reports import BenchmarkReport

    if args.result_action == "list":
        raw_dir = Path(args.dir)
        results_dir = raw_dir if raw_dir.is_absolute() else (bench.workspace / raw_dir)
        if not results_dir.is_dir():
            print(f"results directory not found: {results_dir}")
            return 1
        reports = sorted(results_dir.rglob("report.json"))
        if not reports:
            print("no results found")
            return 0
        rows = []
        for report_path in reports:
            try:
                report = BenchmarkReport.load(report_path)
                summary = report.summary
                run_id = report.id or report_path.parent.name
                status = summary.get("status", "unknown")
                total = str(summary.get("total_cases", 0))
                score = summary.get("average_score")
                score_str = f"{score:.2f}" if score is not None else "-"
                completed = str(summary.get("completed_at", "-"))
                rows.append((run_id, status, total, score_str, completed, str(report_path)))
            except Exception:
                rows.append(("-", "error", "-", "-", "-", str(report_path)))
        headers = ("ID", "STATUS", "CASES", "SCORE", "COMPLETED", "PATH")
        widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        for row in rows:
            print(fmt.format(*row))
        return 0
    if args.result_action == "show":
        raw_path = Path(args.path)
        report_path = raw_path if raw_path.is_absolute() else (bench.workspace / raw_path)
        if not report_path.exists():
            print(f"report not found: {report_path}")
            return 1
        report = BenchmarkReport.load(report_path)
        report.pretty_print()
        return 0
    raise AssertionError(f"unhandled result action: {args.result_action}")


def _cmd_benchmark(bench: NetOpsBench, args: argparse.Namespace) -> int:
    if args.benchmark_action == "prepare":
        scales = _parse_scales(args.scales)
        spec = Path(args.spec) if args.spec else _default_scenario_spec(bench.workspace)
        print("Preparing benchmark assets")
        print(f"  workspace: {bench.workspace}")
        print(f"  scales: {', '.join(scales)}")
        print(f"  spec: {spec}")
        for scale in scales:
            topo_dir = _default_topology_output_dir(bench.workspace, scale)
            scenario_dir = _default_scenario_output_dir(bench.workspace, scale)
            _generate_topology(bench.workspace, scale=scale, output_dir=topo_dir)
            _generate_scenarios(
                bench.workspace, scale=scale, spec=spec, topology_dir=topo_dir, out=scenario_dir, seed=args.seed
            )
        print("Benchmark assets ready")
        return 0
    raise AssertionError(f"unhandled benchmark action: {args.benchmark_action}")


def main() -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    bench = NetOpsBench(workspace=args.workspace)

    if args.command == "status":
        return _cmd_status(bench)
    if args.command == "runtime":
        return _cmd_runtime(bench, args)
    if args.command == "topology":
        return _cmd_topology(bench, args)
    if args.command == "scenario":
        return _cmd_scenario(bench, args)
    if args.command == "result":
        return _cmd_result(bench, args)
    if args.command == "benchmark":
        return _cmd_benchmark(bench, args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
