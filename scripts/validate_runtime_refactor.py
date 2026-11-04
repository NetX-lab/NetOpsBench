#!/usr/bin/env python3
"""Isolated deterministic runtime audit, with no model calls or shortened windows.

For version comparisons, run each snapshot's driver with PYTHONPATH pointing
to that snapshot; this driver only generates recovery policy v2.
One repetition runs a coverage suite with fault/healthy pairs on a fresh lab.
Multiple repetitions measure every requested type (plus healthy) on its own
fresh lab, cold and reused, followed by a healthy check after each fault pair.
Artifacts contain private audit evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from unittest.mock import patch

import netopsbench
from netopsbench.platform.incident.contracts import ToolAction
from netopsbench.platform.observability import influxdb
from netopsbench.platform.runtime.manager import RuntimeManager
from netopsbench.platform.runtime.resources import ResourceMonitor, capacity_check, require_capacity
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.scenario.generator import generate, load_topology
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.utils.control import OperationControl


def select_cases(paths, fault_types, *, all_cases=False, all_mechanisms=False):
    cases = sorted((parse_scenario_file(p) for p in paths), key=lambda case: case.id)
    healthy = next(case for case in cases if case.episode.is_healthy)
    selected = []
    for fault_type in fault_types:
        matches = [case for case in cases if case.episode.fault_type == fault_type]
        if not matches:
            raise ValueError(f"No generated case for {fault_type}")
        if all_mechanisms and not all_cases:
            by_mechanism = {}
            for case in matches:
                by_mechanism.setdefault(case.episode.parameters.get("mechanism", fault_type), case)
            matches = list(by_mechanism.values())
        selected.extend(matches if all_cases or all_mechanisms else matches[:1])
    return healthy, selected


class CommandCounts:
    """Same instrumentation for old/new trees; excludes background processes."""

    def __init__(self):
        self.values = {"subprocess": 0, "docker": 0, "influx_queries": 0}
        self.lock = threading.Lock()
        self.patches = []

    def add(self, key):
        with self.lock:
            self.values[key] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def __enter__(self):
        counts = self
        original_popen = subprocess.Popen
        original_query = influxdb.query_flux

        class CountedPopen(original_popen):
            def __init__(self, args, *pos, **kw):
                counts.add("subprocess")
                if isinstance(args, (list, tuple)) and any(Path(str(arg)).name == "docker" for arg in args[:2]):
                    counts.add("docker")
                super().__init__(args, *pos, **kw)

        def counted_query(*args, **kwargs):
            counts.add("influx_queries")
            return original_query(*args, **kwargs)

        self.query_pair = original_query, counted_query
        self.patches.append(patch.object(subprocess, "Popen", CountedPopen))
        # Replace already imported aliases too; later imports use the patched module.
        for name, module in list(sys.modules.items()):
            if name.startswith("netopsbench.") and module is not None:
                for key, value in list(vars(module).items()):
                    if value is original_query:
                        self.patches.append(patch.object(module, key, counted_query))
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *_):
        original_query, counted_query = self.query_pair
        # Lazy imports during a batch must not retain that batch's counter.
        for name, module in list(sys.modules.items()):
            if name.startswith("netopsbench.") and module is not None:
                for key, value in list(vars(module).items()):
                    if value is counted_query:
                        setattr(module, key, original_query)
        for item in reversed(self.patches):
            item.stop()


def deterministic_diagnosis(episode_result, *, diagnostic_session, diagnostic_payload, tool_profile="full"):
    calls = [("get_topology", {}), ("get_pingmesh_summary", {})]
    devices = diagnostic_payload["topology"]["devices"]
    switches = sorted({row["name"] for group, rows in devices.items() if group != "clients" for row in rows})
    if tool_profile == "full":
        calls.extend(
            (name, {"device": device})
            for device in switches
            for name in ("get_device_interfaces", "get_bgp_neighbors", "get_route_table", "get_device_acl")
        )
    observations = []
    for name, arguments in calls:
        result = diagnostic_session.call_tool(ToolAction(name=name, arguments=arguments))
        observations.append({"name": name, "arguments": arguments, "result": result.model_dump(mode="json")})
    # This audits execution/evidence, not classifier accuracy. Never infer health
    # from absent diagnosis or use private ground truth as a diagnosis.
    return {"verdict": "inconclusive", "success": True, "metadata": {"scripted_observations": observations}}


def verify_residual_rejected(runner, fault_type):
    """Suppress undo writes only; all verification reads hit the real devices."""
    from netopsbench.platform.faults.handlers.network import _BGPD_RESTORE_SOURCE

    if fault_type == "device_down":
        return {"skipped": "Containerlab node activation is outside the command hook"}
    original = runner.injector._cmd.docker_exec
    suppressed = []

    def no_undo(container, commands, *args, **kwargs):
        mutation = (
            commands[:1] == ["config"]
            or commands[:3] == ["tc", "qdisc", "del"]
            or commands[:3] == ["ip", "link", "set"]
            or commands[:2] == ["iptables", "-D"]
            or commands[:3] == ["sonic-db-cli", "CONFIG_DB", "del"]
            or commands[:3] == ["python3", "-c", _BGPD_RESTORE_SOURCE]
            or (commands[:1] == ["vtysh"] and "configure terminal" in commands)
        )
        if mutation:
            suppressed.append(commands[0])
            return subprocess.CompletedProcess(commands, 0, "", "")
        return original(container, commands, *args, **kwargs)

    if fault_type in {"link_flapping", "route_flapping"}:
        with patch.object(runner.injector._tracker, "stop_background", return_value=False):
            results = runner.injector.recover_all()
    else:
        with patch.object(runner.injector._cmd, "docker_exec", side_effect=no_undo):
            results = runner.injector.recover_all()
        if not suppressed:
            raise RuntimeError("Negative recovery test suppressed no writes")
    if not results or all(item.get("recovered") for item in results) or not runner.injector.active_faults:
        raise RuntimeError("Recovery incorrectly accepted an unremoved real fault")
    return {"passed": True, "suppressed_write_count": len(suppressed), "results": results}


def verify_shadow(runner, case):
    """Audit data-plane health without publishing a production baseline."""
    from copy import deepcopy

    started = time.monotonic()
    baseline = deepcopy(runner._baseline_handoff)
    last_calibration = runner._last_calibration
    calibration_current = runner._calibration_current
    try:
        runner.calibrate(case, "shadow_validation")
        return {"seconds": time.monotonic() - started, **runner._last_calibration}
    finally:
        runner._baseline_handoff = baseline
        runner._last_calibration = last_calibration
        runner._calibration_current = calibration_current


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    package = Path(netopsbench.__file__).resolve().parent
    source_hash = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        source_hash.update(str(path.relative_to(package)).encode())
        source_hash.update(path.read_bytes())
    report = {
        "scale": args.scale,
        "recovery_policy": {"version": 2, "baseline_max_cases": 10, "baseline_max_age_seconds": 600},
        "tool_profile": args.tool_profile,
        "repetitions": [],
        "status": "running",
        "source_sha256": source_hash.hexdigest(),
        "source_root": str(package),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }

    def save():
        (output / "audit.json").write_text(json.dumps(report, indent=2, default=str))

    manager = RuntimeManager(workspace=str(output))
    save()
    batches = (
        [(repetition, kind) for repetition in range(args.repeats) for kind in ("none", *args.fault_types)]
        if args.repeats > 1
        else [(repetition, None) for repetition in range(args.batch_repeats)]
    )
    for repetition, cold_type in batches:
        record = {"repetition": repetition + 1, "cold_type": cold_type, "cases": []}
        report["repetitions"].append(record)
        if args.scale != "xs":
            if args.capacity_sample is None:
                raise ValueError("medium requires --capacity-sample from a completed XS acceptance")
            docker_root = Path(
                subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip()
            )
            record["capacity"] = capacity_check(
                json.loads(args.capacity_sample.read_text()), args.scale, docker_root, False, True
            )
            save()
            require_capacity(record["capacity"], False)
        batch_started = time.monotonic()
        runtime = manager.create(scale=args.scale, name=f"audit-{args.scale}-{time.time_ns():x}")
        record["runtime"] = str(runtime.root_dir)
        runner = None
        cancel, emergency = threading.Event(), threading.Event()
        monitor = ResourceMonitor(record, cancel, emergency, threading.RLock())
        runtime.operation_control = OperationControl(cancel)
        try:
            monitor.start()
            with CommandCounts() as counts:
                started = time.monotonic()
                runtime.deploy().ensure_observability().ensure_pingmesh().warm()
                record["deploy"] = {"seconds": time.monotonic() - started, "operations": counts.snapshot()}
                record["runtime_spec"] = runtime.describe()
                worker = runtime.workers[0]
                paths = generate(
                    {"fault_catalog_version": 2, "recovery_policy": {"version": 2}},
                    load_topology(args.scale, str(worker.topology_dir)),
                    output / f"scenarios-{repetition + 1}-{cold_type or 'suite'}",
                    seed=42,
                )
                healthy, faults = select_cases(
                    paths, args.fault_types, all_cases=args.all_cases, all_mechanisms=args.all_mechanisms
                )
                runner = ScenarioExecutor(
                    topology_dir=str(worker.topology_dir),
                    topology_id=worker.topology_id,
                    influxdb_bucket=worker.bucket,
                    runtime_worker=worker,
                    scale_registry=runtime.scale_registry,
                )
                runner._cancel_event = cancel
                sequence = [healthy]
                for fault in faults:
                    sequence.extend([fault, healthy])
                if cold_type is not None:
                    case = (
                        healthy
                        if cold_type == "none"
                        else next(case for case in faults if case.episode.fault_type == cold_type)
                    )
                    sequence = [case, case] if cold_type == "none" else [case, healthy, case, healthy]
                if args.shadow:
                    sequence = [healthy, *faults, healthy]
                elif args.compact_sequence:
                    sequence = [healthy, *faults]
                sequence = sequence * args.sequence_repeats

                sequence_started = time.monotonic()
                for index, case in enumerate(sequence):
                    before = counts.snapshot()
                    started = time.monotonic()
                    negative_checks = []
                    recover = runner._recover_fault

                    def audit_recovery(case=case, negative_checks=negative_checks, recover=recover, runner=runner):
                        if args.readback_negatives and not case.episode.is_healthy and not negative_checks:
                            negative_checks.append(verify_residual_rejected(runner, case.episode.fault_type))
                        return recover()

                    with patch.object(runner, "_recover_fault", side_effect=audit_recovery):
                        result = runner.run_scenario(
                            case, diagnosis_callback=partial(deterministic_diagnosis, tool_profile=args.tool_profile)
                        )
                    item = {
                        "fault_type": case.episode.fault_type,
                        "cold": index == 0,
                        "seconds": time.monotonic() - started,
                        "operations": {key: value - before[key] for key, value in counts.snapshot().items()},
                        "result": result,
                        "negative_readbacks": negative_checks,
                    }
                    if args.shadow and result.get("success"):
                        item["shadow"] = verify_shadow(runner, case)
                    record["cases"].append(item)
                    save()
                    if not result.get("case_valid") or not result.get("success") or result.get("cleanup_failed"):
                        raise RuntimeError(f"Case validation/recovery failed: {case.id}")
                before = counts.snapshot()
                final_started = time.monotonic()
                calibration = runner.final_calibration(sequence[-1], destroy_after_run=True)
                record["final_calibration"] = {
                    "seconds": time.monotonic() - final_started,
                    "result": calibration,
                    "operations": {key: value - before[key] for key, value in counts.snapshot().items()},
                }
                record["sequence_seconds"] = time.monotonic() - sequence_started
                save()
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            report["status"] = "failed"
            raise
        finally:
            try:
                if runner is not None:
                    runner.close()
            finally:
                teardown_started = time.monotonic()
                try:
                    runtime.teardown()
                    record["disposition"] = "torn_down"
                except Exception as exc:
                    record["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                    record["disposition"] = "quarantined"
                    report["status"] = "failed"
                    runtime.mark_quarantined("audit_cleanup_failed")
                record["teardown_seconds"] = time.monotonic() - teardown_started
                record["batch_seconds"] = time.monotonic() - batch_started
                save()
                monitor.close()
        if report["status"] == "failed":
            return 1
    report["status"] = "passed"
    save()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", required=True)
    parser.add_argument("--all-mechanisms", action="store_true")
    parser.add_argument("--sequence-repeats", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--tool-profile", choices=["full", "minimal"], default="full")
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--readback-negatives", action="store_true")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument(
        "--compact-sequence",
        action="store_true",
        help="Batch health and selected faults without interleaved healthy cases",
    )
    parser.add_argument("--fault-types", nargs="+", required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    parser.add_argument(
        "--batch-repeats",
        type=int,
        choices=range(1, 4),
        default=1,
        help="Repeat the entire sequence on independent fresh runtimes",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capacity-sample", type=Path)
    args = parser.parse_args()
    if args.repeats > 1 and args.batch_repeats > 1:
        parser.error("Choose either per-type --repeats or whole-suite --batch-repeats")
    if args.compact_sequence and args.repeats > 1:
        parser.error("--compact-sequence requires whole-suite repetitions")
    raise SystemExit(run(args))
