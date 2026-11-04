#!/usr/bin/env python3
"""Isolated deterministic runtime audit, with no model calls or shortened windows.

Run with PYTHONPATH pointing to either source tree for comparable measurements.
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


def select_cases(paths, fault_types):
    cases = sorted((parse_scenario_file(p) for p in paths), key=lambda case: case.id)
    healthy = next(case for case in cases if case.episode.is_healthy)
    selected = []
    for fault_type in fault_types:
        matches = [case for case in cases if case.episode.fault_type == fault_type]
        if not matches:
            raise ValueError(f"No generated case for {fault_type}")
        selected.extend(matches[:1])
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
        for item in reversed(self.patches):
            item.stop()


def deterministic_diagnosis(episode_result, *, diagnostic_session, diagnostic_payload):
    calls = [("get_topology", {}), ("get_pingmesh_summary", {})]
    devices = diagnostic_payload["topology"]["devices"]
    switches = sorted({row["name"] for group, rows in devices.items() if group != "clients" for row in rows})
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
        else [(0, None)]
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
                    {"fault_catalog_version": 2},
                    load_topology(args.scale, str(worker.topology_dir)),
                    output / f"scenarios-{repetition + 1}-{cold_type or 'suite'}",
                    seed=42,
                )
                healthy, faults = select_cases(paths, args.fault_types)
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
                for index, case in enumerate(sequence):
                    before = counts.snapshot()
                    started = time.monotonic()
                    result = runner.run_scenario(case, diagnosis_callback=deterministic_diagnosis)
                    item = {
                        "fault_type": case.episode.fault_type,
                        "cold": index == 0,
                        "seconds": time.monotonic() - started,
                        "operations": {key: value - before[key] for key, value in counts.snapshot().items()},
                        "result": result,
                    }
                    record["cases"].append(item)
                    save()
                    if not result.get("case_valid") or not result.get("success") or result.get("cleanup_failed"):
                        raise RuntimeError(f"Case validation/recovery failed: {case.id}")
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            report["status"] = "failed"
            raise
        finally:
            try:
                if runner is not None:
                    runner.close()
            finally:
                try:
                    runtime.teardown()
                    record["disposition"] = "torn_down"
                except Exception as exc:
                    record["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                    record["disposition"] = "quarantined"
                    report["status"] = "failed"
                    runtime.mark_quarantined("audit_cleanup_failed")
                save()
                monitor.close()
        if report["status"] == "failed":
            return 1
    report["status"] = "passed"
    save()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=["xs", "medium"], required=True)
    parser.add_argument("--fault-types", nargs="+", required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capacity-sample", type=Path)
    raise SystemExit(run(parser.parse_args()))
