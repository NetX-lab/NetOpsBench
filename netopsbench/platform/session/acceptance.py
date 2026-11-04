"""Internal acceptance orchestration over the canonical suite execution path."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from netopsbench.config import config
from netopsbench.evaluator.scorer import Evaluator
from netopsbench.models.profiles import get_scale_profile
from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.faults.catalog import FAULT_CATALOG_VERSION
from netopsbench.platform.observability.bgp_collector import (
    DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS,
    DEFAULT_BGP_POLL_INTERVAL_SECONDS,
)
from netopsbench.platform.observability.influxdb import query_flux
from netopsbench.platform.runtime.health import check_worker_health, expected_network_counts
from netopsbench.platform.runtime.resources import (
    ResourceMonitor,
    capacity_check,
    peak_sample,
    require_capacity,
    storage_snapshot,
)
from netopsbench.platform.runtime.toolchain import runtime_backend_identity
from netopsbench.platform.scenario.generator import generate, load_topology, load_yaml
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.session.dispatch import ExecutionControl, is_recovered_case_failure
from netopsbench.platform.topology.config import config_for_scale
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.control import OperationControl
from netopsbench.platform.utils.files import atomic_write_json

_CHECKPOINT_LOCK = threading.RLock()


def ingestion_latency(worker, plan_id: str) -> dict:
    """Measure write-to-query visibility in this runtime's own bucket."""
    stamp = time.time_ns()
    started = time.monotonic()
    response = requests.post(
        f"{config.influxdb_url.rstrip('/')}/api/v2/write",
        params={"org": config.influxdb_org, "bucket": worker.bucket, "precision": "ns"},
        headers={"Authorization": f"Token {config.influxdb_token}"},
        data=f"acceptance_ingestion_probe,plan_id={plan_id} value=1i {stamp}",
        proxies={"http": "", "https": ""},
        timeout=30,
    )
    response.raise_for_status()
    write_seconds = time.monotonic() - started
    query = (
        f'from(bucket: "{worker.bucket}") |> range(start: time(v: {stamp})) '
        f'|> filter(fn: (r) => r._measurement == "acceptance_ingestion_probe" and r.plan_id == "{plan_id}") '
        "|> limit(n: 1)"
    )
    while time.monotonic() - started < 30:
        result = query_flux(config.influxdb_url, config.influxdb_token, config.influxdb_org, query)
        if result.status == "ok" and "acceptance_ingestion_probe" in result.text:
            return {"write_seconds": write_seconds, "query_visible_seconds": time.monotonic() - started}
        time.sleep(0.2)
    raise RuntimeError("Runtime ingestion probe was not queryable within thirty seconds")


def bgp_observation(worker, manifest, output: Path, *, control: OperationControl | None = None) -> dict:
    freshness_seconds = int(2 * DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS + DEFAULT_BGP_POLL_INTERVAL_SECONDS)
    query = (
        f'from(bucket: "{worker.bucket}") |> range(start: -{freshness_seconds}s) '
        f'|> filter(fn: (r) => r._measurement == "bgp_neighbors" and r.topology_id == "{worker.topology_id}" '
        'and r._field == "session_state") |> last() '
        '|> group(columns: ["source", "neighbor_address"]) |> sort(columns: ["_time"]) |> last() |> group() '
        '|> keep(columns: ["source", "neighbor_address", "_value", "_time"])'
    )
    while True:
        result = query_flux(
            config.influxdb_url,
            config.influxdb_token,
            config.influxdb_org,
            query,
            timeout=control.remaining(60) if control else 60,
            deadline=control.deadline if control else None,
        )
        if result.status != "ok":
            raise RuntimeError(f"BGP observation query failed: {result.error}")
        output.write_text(result.text)
        rows = csv.DictReader(
            io.StringIO("\n".join(line for line in result.text.splitlines() if not line.startswith("#")))
        )
        neighbors = {
            (row.get("source"), row.get("neighbor_address")): row.get("_value", "")
            for row in rows
            if row.get("_time") not in {None, "", "_time"}
        }
        expected = sum(row["bgp_neighbors"] for row in expected_network_counts(manifest).values())
        established = sum(state.lower() == "established" for state in neighbors.values())
        if len(neighbors) == expected and established == expected:
            break
        message = (
            f"Incomplete BGP observations: {len(neighbors)} neighbors, {established} established, expected {expected}"
        )
        if control is None:
            raise RuntimeError(message)
        control.sleep(DEFAULT_BGP_POLL_INTERVAL_SECONDS)
    return {
        "neighbors": len(neighbors),
        "established": established,
        "expected": expected,
        "freshness_seconds": freshness_seconds,
    }


def finished_case_ids(entry: dict) -> set[str]:
    """Skip scored diagnoses and fully recovered case failures already attempted."""
    return {
        summary["scenario_id"]
        for report in entry.get("cases", [])
        if not report.get("acceptance_error")
        for summary in report.get("scenario_summaries", [])
        if (report.get("mode") != "effects" and is_recovered_case_failure(summary))
        or (
            summary.get("case_valid") is True
            and (
                (
                    report.get("mode") == "effects"
                    and not summary.get("cleanup_failed")
                    and summary.get("status") == "completed"
                )
                or (report.get("mode") != "effects" and report.get("diagnosis_complete") is True)
            )
        )
    }


def checkpoint(output, result):
    with _CHECKPOINT_LOCK:
        atomic_write_json(output / "acceptance.json", result, default=str)


def freeze_cases(records, paths):
    cases = [parse_scenario_file(path) for path in paths]
    hashes = {case.id: hashlib.sha256(path.read_bytes()).hexdigest() for case, path in zip(cases, paths, strict=True)}
    if len(hashes) != len(cases):
        raise ValueError("Acceptance scenario IDs must be unique")
    selected = records.get("selected_cases")
    if selected is not None and set(selected) != set(hashes):
        raise ValueError("Acceptance scenario set changed; use a separate campaign")
    previous = records.get("scenario_sha256")
    if previous is not None and previous != hashes:
        raise ValueError("Acceptance scenario contents changed; use a separate campaign")
    records.setdefault("selected_cases", list(hashes))
    records.setdefault("scenario_sha256", hashes)
    return cases


def expected_identities(result, backends):
    identities = {}
    for entry in result["scales"].values():
        for backend, record in entry.get("agents", {}).items():
            if backend in backends:
                if backend in identities and identities[backend] != record["identity"]:
                    raise ValueError("Campaign contains inconsistent agent configurations")
                identities[backend] = record["identity"]
    return identities


def qualified_codex_smoke(args, identities, backend_identity):
    """Reuse only the exact qualification receipt frozen by the campaign driver."""
    path = getattr(args, "codex_smoke_receipt", None)
    if path is None:
        return None
    payload = Path(path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != getattr(args, "codex_smoke_receipt_sha256", None):
        raise ValueError("Codex qualification receipt fingerprint changed")
    receipt = json.loads(payload)
    recorded_agent = receipt.get("agent_identity") or {}
    actual_agent = identities.get("codex") or {}
    # Docker's classic and containerd stores can expose different image IDs for
    # an identical imported image. Keep both actual IDs and compare its content.
    recorded_config = {key: value for key, value in recorded_agent.items() if key != "image_id"}
    actual_config = {key: value for key, value in actual_agent.items() if key != "image_id"}
    if (
        receipt.get("schema_version") != 1
        or receipt.get("status") != "passed"
        or receipt.get("runtime_disposition") != "torn_down"
        or receipt.get("final_health", {}).get("status") != "passed"
        or receipt.get("runtime_backend") != backend_identity
        or not identities.get("codex")
        or not recorded_agent.get("image_content_sha256")
        or recorded_config != actual_config
        or not receipt.get("source_tree_sha256")
        or receipt.get("source_tree_sha256") != getattr(args, "source_tree_sha256", None)
    ):
        raise ValueError("Codex qualification configuration or final health differs")
    cases = receipt.get("cases", [])
    if (
        len(cases) != 4
        or len({case.get("id") for case in cases}) != 4
        or {case.get("fault_type") for case in cases} != {"none", "link_down", "device_down", "bgp_auth_mismatch"}
        or not all(
            case.get("valid") is True
            and case.get("diagnosis_complete") is True
            and case.get("status") == "completed"
            and not case.get("cleanup_failed")
            for case in cases
        )
    ):
        raise ValueError("Codex qualification requires four completed, valid XS cases")
    return {"sha256": digest, **receipt, "execution_image_id": actual_agent["image_id"]}


def validate_campaign_cases(entry, scale, backends, output, runtime, select_cases, smoke_cases):
    """Reject changes before provider calls, warm, teardown or redeployment."""
    if entry.get("runtime_backend") != runtime_backend_identity():
        raise ValueError("Campaign runtime backend changed or is unrecorded; start a separate campaign")
    if entry.get("profile_sha256") != get_scale_profile(scale).digest:
        raise ValueError("Resumed campaign profile differs from the accepted cases")
    records = entry.get("agents", {})
    if entry.get("cases") or not records:
        raise ValueError("Legacy acceptance checkpoint; retain it as an archive and start a new campaign")
    if set(records) != set(backends):
        raise ValueError("Acceptance agent set changed; use a separate campaign")
    for record in records.values():
        if "selected_cases" not in record or "scenario_sha256" not in record:
            raise ValueError("Checkpoint lacks immutable case identity; start a separate campaign")
    if entry.get("client_images"):
        image = config_for_scale(scale).client_image
        actual = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True, timeout=30
        ).strip()
        if entry["client_images"] != [actual]:
            raise ValueError("Resumed client image differs from the accepted cases")
    if runtime is not None and (runtime.workers[0].topology_dir / "topology.json").exists():
        manifest = load_topology_manifest(runtime.workers[0].topology_dir)
    elif entry.get("manifest"):
        manifest = TopologyManifest.model_validate(entry["manifest"])
    else:
        raise ValueError("Checkpoint lacks topology identity; start a separate campaign")
    if entry.get("plan") and manifest.require_pingmesh_plan().settings.plan_id != entry["plan"]["plan_id"]:
        raise ValueError("Resumed campaign Pingmesh plan changed")
    paths = sorted((output / "scenarios" / scale).glob("*.yaml"))
    selected = (
        smoke_cases(paths, manifest)
        if scale == "xs" and entry.get("smoke_cases")
        else select_cases(paths, True, manifest)
    )
    for record in records.values():
        freeze_cases(record, selected)


def validate_resume(entry, runtime, scale, *, allow_unplanned=False):
    identity = runtime_backend_identity()
    if entry.get("runtime_backend") != identity or runtime.metadata.get("runtime_backend") != identity:
        raise ValueError("Resumed runtime backend changed or is unrecorded; start a separate campaign")
    if runtime.scale != scale or len(runtime.workers) != 1:
        raise ValueError("Resume runtime must match the scale and contain exactly one worker")
    digest = entry.get("profile_sha256")
    if digest is not None and digest != runtime.scale_registry.get(scale).digest:
        raise ValueError("Resumed runtime profile differs from the accepted cases")
    if allow_unplanned and not entry.get("plan"):
        return None
    plan = load_topology_manifest(runtime.workers[0].topology_dir).require_pingmesh_plan()
    if entry.get("plan", {}).get("plan_id") != plan.settings.plan_id:
        raise ValueError("Resumed runtime Pingmesh plan differs from the accepted cases")
    return plan


def release_runtime(runtime, *, owned, completed, emergency=False):
    """Only an explicitly owned, completed (or resource-emergency) lab is released."""
    if owned and (completed or emergency):
        runtime.teardown()
        return "torn_down"
    return "quarantined" if runtime.metadata.get("quarantined") else "retained"


def quarantine_partial_deployment(runtime, entry, output):
    """Retain an owned incomplete deployment and its logs for explicit rebuild."""
    deploy = runtime.stage_results.get("deploy")
    if not entry.get("runtime_owned") or deploy is None or deploy.status == "completed":
        return False
    entry["partial_deployment"] = runtime.describe()
    logs = runtime.root_dir / "logs"
    if logs.exists():
        shutil.copytree(logs, output / "failed-deployments" / runtime.name, dirs_exist_ok=True)
    runtime.mark_quarantined("incomplete_deployment")
    entry["runtime_disposition"] = "quarantined"
    return True


def require_full_health(runtime, cancel=None):
    errors = check_worker_health(
        runtime.workers[0], all_routing_devices=True, scale_registry=runtime.scale_registry, cancel_event=cancel
    )
    if errors:
        runtime.mark_quarantined("acceptance_health_failure")
        raise RuntimeError("Full health failed: " + "; ".join(errors))
    return {
        "status": "passed",
        "time": datetime.now(UTC).isoformat(),
        "all_routing_devices": True,
        "switches_checked": runtime.scale_registry.get(runtime.scale).total_switches,
    }


def run_cases(bench, runtime, paths, *, backend, vendor, entry, identity, output, result, cancel, agent_factory=None):
    records = entry.setdefault("agents", {}).setdefault(backend, {"cases": [], "identity": identity})
    if records["identity"] != identity:
        raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
    cases = freeze_cases(records, paths)
    completed = finished_case_ids(records)
    pending = [case for case in cases if case.id not in completed]
    if not pending:
        return False
    effects_only = backend == "effects"
    raw_agent = (
        None
        if effects_only
        else (
            agent_factory(backend)(vendor=vendor)
            if backend == "deepagent"
            else agent_factory(backend)(artifacts_dir=output / "codex-traces" / runtime.scale)
        )
    )
    agent = None if effects_only else bench.agents.wrap(raw_agent)

    def on_case(summary, raw, evaluations):
        aggregate = (
            {"mode": "effects", "effect_audit": (raw.get("episode") or {}).get("effect_audit")}
            if effects_only
            else Evaluator().generate_report(evaluations, agent_name=agent.name, topology_scale=runtime.scale)
        )
        if not effects_only:
            episode = raw.get("episode") or {}
            execution = episode.get("execution") or {}
            aggregate["diagnosis_complete"] = (
                (episode.get("diagnosis") or {}).get("success") is True
                and execution.get("case_valid") is True
                and execution.get("termination_reason") == "submitted"
            )
        records["cases"].append({**aggregate, "scenario_summaries": [summary]})
        records["last_recovery"] = raw.get("cleanup")
        print(f"[{runtime.scale}/{backend}] {summary['scenario_id']}: {summary['status']}", flush=True)
        checkpoint(output, result)

    report = bench.sessions._executor._run_with_existing_runtime(
        mode="effects" if effects_only else "suite",
        scenarios=pending,
        runtime=runtime,
        agent=agent,
        artifacts_dir=output / "runs" / runtime.scale / backend,
        trace=True,
        control=ExecutionControl(on_case=on_case, stop_on_failure=True, cancel_event=cancel, effects_only=effects_only),
    ).wait()
    records.setdefault("suite_reports", []).append(report.artifact_paths["report"])
    checkpoint(output, result)
    if cancel.is_set() or any(
        row.get("status") != "completed" and not is_recovered_case_failure(row) for row in report.scenario_summaries
    ):
        raise RuntimeError(f"{backend} acceptance interrupted; runtime retained after fault recovery")
    return True


def run_acceptance(args, *, bench_factory, agent_factory, preflight, select_cases, smoke_cases) -> int:
    mode = getattr(args, "mode", "diagnose")
    backends_requested = ["effects"] if mode == "effects" else args.agents
    scales = args.scales.split(",")
    if args.resume_runtime and len(scales) != 1:
        raise ValueError("--resume-runtime requires exactly one scale")
    if args.rebuild_runtime and not args.resume_runtime:
        raise ValueError("--rebuild-runtime requires --resume-runtime")
    backend_identity = runtime_backend_identity()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = (
        json.loads((output / "acceptance.json").read_text())
        if args.resume_runtime
        else {
            "started": datetime.now(UTC).isoformat(),
            "vendor": args.vendor,
            "scales": {},
            "mode": mode,
            "fault_catalog_version": FAULT_CATALOG_VERSION,
        }
    )
    if args.resume_runtime:
        if result.get("runtime_backend") != backend_identity:
            raise ValueError("Campaign runtime backend changed or is unrecorded; start a separate campaign")
        if result.get("mode") != mode or result.get("fault_catalog_version") != FAULT_CATALOG_VERSION:
            raise ValueError("Campaign mode or fault catalog changed; start a separate campaign")
        if result["vendor"] != args.vendor:
            raise ValueError("Cannot change the diagnosis vendor when resuming acceptance")
        recorded = result["scales"].get(scales[0])
        if recorded is None or Path(recorded["runtime"]).resolve() != args.resume_runtime.resolve():
            raise ValueError("Resume runtime must match the recorded acceptance runtime")
        for item in (result, recorded):
            if item.get("error"):
                item.setdefault("prior_errors", []).append(
                    {"time": datetime.now(UTC).isoformat(), "error": item.pop("error")}
                )
        result.setdefault("resumptions", []).append(
            {
                "time": datetime.now(UTC).isoformat(),
                "runtime": str(args.resume_runtime.resolve()),
                "previous_status": result.get("status"),
                "previous_finished": result.get("finished"),
            }
        )
    result["runtime_backend"] = backend_identity
    sample_path = args.capacity_sample or output / "capacity-sample.json"
    sample = json.loads(sample_path.read_text()) if sample_path.exists() else None
    docker_root = Path(subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip())
    # A rebuild's full capacity check runs after its owned old lab is released.
    # All new allocations remain gated below, before runtime creation.
    if sample is not None and not args.rebuild_runtime:
        existing_metadata = args.resume_runtime / "runtime.json" if args.resume_runtime else None
        retaining = (
            existing_metadata is not None
            and existing_metadata.exists()
            and json.loads(existing_metadata.read_text()).get("state") == "warm"
            and not args.rebuild_runtime
        )
        for scale in scales:
            capacity = capacity_check(sample, scale, docker_root, retaining, mode == "diagnose")
            require_capacity(capacity, args.allow_tight_capacity)
    cancel, emergency = threading.Event(), threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: cancel.set())
    control = OperationControl(cancel)
    monitor = ResourceMonitor(result, cancel, emergency, _CHECKPOINT_LOCK)
    active_runtime, active_entry = None, None
    try:
        monitor.start()
        control.remaining()
        with bench_factory(workspace=str(Path.cwd())) as bench:
            # Reject a live resume mismatch before health checks, model requests or mutations.
            resumed = None
            if args.resume_runtime and (args.resume_runtime / "runtime.json").exists():
                resumed = bench.runtimes.attach(args.resume_runtime)._runtime
                validate_resume(recorded, resumed, scales[0], allow_unplanned=args.rebuild_runtime)
                active_runtime, active_entry = resumed, recorded
                if resumed.metadata.get("quarantined") and not args.rebuild_runtime:
                    raise RuntimeError("Runtime is quarantined; repair it or explicitly use --rebuild-runtime")
                if args.rebuild_runtime:
                    if not recorded.get("runtime_owned", False):
                        raise ValueError("Cannot rebuild an externally owned runtime")
            if args.resume_runtime:
                validate_campaign_cases(
                    recorded, scales[0], backends_requested, output, resumed, select_cases, smoke_cases
                )
            if mode == "effects":
                # An empty backend list validates images/toolchain without agents,
                # credential writes or model requests.
                preflight([], args.vendor, output)
                control.remaining()
            identities = (
                {"effects": {"mode": mode, "fault_catalog_version": FAULT_CATALOG_VERSION}}
                if mode == "effects"
                else preflight(args.agents, args.vendor, output, expected=expected_identities(result, args.agents))
            )
            control.remaining()
            for backend, identity in identities.items():
                for scale in scales:
                    old = result["scales"].get(scale, {}).get("agents", {}).get(backend, {}).get("identity")
                    if old is not None and old != identity:
                        raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
            if args.rebuild_runtime and resumed is not None:
                resumed.teardown()
                resumed = None
            qualification = qualified_codex_smoke(args, identities, backend_identity)
            if qualification is not None:
                previous_qualification = result.get("codex_qualification")
                if previous_qualification is not None and previous_qualification["sha256"] != qualification["sha256"]:
                    raise ValueError("Cannot change Codex qualification when resuming acceptance")
                result["codex_qualification"] = qualification
            needs_smoke = (
                "codex" in backends_requested
                and "xs" not in scales
                and not result.get("codex_xs_smoke")
                and qualification is None
            )
            phases = (["xs"] if needs_smoke or sample is None or "xs" in scales else []) + [
                scale for scale in scales if scale != "xs"
            ]
            for scale in phases:
                control.remaining()
                current = result["scales"].setdefault(scale, {"resource_samples": []})
                entry = current
                completed = False
                if sample is not None and scale != "xs":
                    entry["capacity"] = capacity_check(
                        sample,
                        scale,
                        docker_root,
                        resumed is not None and scale == scales[0] and resumed.state == "warm",
                        mode == "diagnose",
                    )
                    require_capacity(entry["capacity"], args.allow_tight_capacity)
                    if entry["capacity"]["status"] != "passed":
                        entry["capacity_override"] = "explicitly_authorized_twenty_percent_memory_margin_waiver"
                runtime = resumed if resumed is not None and scale == scales[0] else None
                if runtime is None:
                    token = datetime.now(UTC).strftime("%m%d%H%M%S")
                    rebuilding = args.resume_runtime is not None and scale == scales[0]
                    if rebuilding and not args.rebuild_runtime:
                        if entry.get("historical_redeployment"):
                            raise ValueError("Historical runtime has already been redeployed once")
                        entry["historical_redeployment"] = True
                    if rebuilding and (
                        not entry.get("runtime_owned")
                        or args.resume_runtime.resolve() != bench.runtimes.runtime_root_dir / args.resume_runtime.name
                    ):
                        raise ValueError("Only an owned runtime in the managed runtime directory can be rebuilt")
                    runtime = bench.runtimes.create(
                        scale=scale,
                        workers=1,
                        name=args.resume_runtime.name if rebuilding else f"pmv2-{scale}-{token}",
                    )._runtime
                    if entry.get("runtime"):
                        entry.setdefault("prior_runtimes", []).append(entry["runtime"])
                    entry.update(runtime=str(runtime.root_dir), runtime_owned=True)
                active_runtime, active_entry = runtime, entry
                control.remaining()
                digest = runtime.scale_registry.get(scale).digest
                if entry.get("profile_sha256", digest) != digest:
                    raise ValueError("Runtime profile differs from the accepted cases")
                entry.update(
                    status="running",
                    runtime_disposition="active",
                    profile_sha256=digest,
                    runtime_spec=runtime.describe(),
                    mode=mode,
                    fault_catalog_version=FAULT_CATALOG_VERSION,
                    runtime_backend=backend_identity,
                )
                result["status"] = "running"
                result.pop("finished", None)
                checkpoint(output, result)
                worker = runtime.workers[0]
                preflight_topology = output / "preflight-topology" / scale
                if not (preflight_topology / "topology.json").exists():
                    generate_topology(
                        scale,
                        str(preflight_topology),
                        name=worker.lab_name,
                        mgmt_subnet=worker.mgmt_subnet,
                        mgmt_network=worker.mgmt_network,
                    )
                manifest = load_topology_manifest(preflight_topology)
                existing = sorted((output / "scenarios" / scale).glob("*.yaml"))
                paths = existing or generate(
                    load_yaml(Path.cwd() / "netopsbench/platform/scenario/specs/fault_campaign.yaml"),
                    load_topology(scale, str(preflight_topology)),
                    output / "scenarios" / scale,
                    seed=42,
                )
                selected = (
                    smoke_cases(paths, manifest)
                    if entry.get("smoke_cases", scale == "xs" and needs_smoke)
                    else select_cases(paths, True, manifest)
                )
                backends = ["codex"] if scale == "xs" and needs_smoke else backends_requested
                entry.setdefault("smoke_cases", bool(scale == "xs" and needs_smoke))
                for backend in backends:
                    record = entry.setdefault("agents", {}).setdefault(
                        backend,
                        {
                            "identity": identities[backend],
                            "cases": [],
                        },
                    )
                    freeze_cases(record, selected)
                entry.setdefault("manifest", manifest.model_dump(mode="json"))
                checkpoint(output, result)
                runtime.operation_control = control
                worker = runtime.workers[0]
                monitor.watch(entry, worker.lab_name, worker.topology_dir)
                try:
                    if runtime.state != "warm":
                        runtime.deploy().ensure_observability().ensure_pingmesh()
                    worker = runtime.workers[0]
                    manifest = load_topology_manifest(worker.topology_dir)
                    plan = manifest.require_pingmesh_plan()
                    if entry.get("plan") and entry["plan"]["plan_id"] != plan.settings.plan_id:
                        raise ValueError("Rebuilt runtime Pingmesh plan differs from the accepted cases")
                    entry["plan"] = plan.summary()
                    names = [f"clab-{worker.lab_name}-{client.name}" for client in manifest.clients()]
                    images = sorted(
                        set(
                            subprocess.check_output(
                                ["docker", "inspect", "--format", "{{.Image}}", *names], text=True, timeout=60
                            ).split()
                        )
                    )
                    if entry.get("client_images") and entry["client_images"] != images:
                        raise ValueError("Runtime client image differs from the accepted cases")
                    entry["client_images"] = images
                    entry["manifest"] = manifest.model_dump(mode="json")
                    if runtime.state == "warm":
                        entry["start_health"] = require_full_health(runtime, cancel)
                    else:
                        runtime.warm()
                        entry["start_health"] = runtime.stage_results["warm"].details
                    (output / f"{scale}-coverage.json").write_text(plan.coverage.model_dump_json(indent=2))
                    entry["ingestion_latency"] = ingestion_latency(worker, plan.settings.plan_id)
                    entry["bgp_observation"] = bgp_observation(
                        worker,
                        manifest,
                        output / f"{scale}-bgp.csv",
                        control=OperationControl(
                            cancel,
                            time.monotonic()
                            + 2 * DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS
                            + DEFAULT_BGP_POLL_INTERVAL_SECONDS,
                        ),
                    )
                    entry.setdefault("storage_start", storage_snapshot())
                    # Keep prepared identities durable even if the process exits
                    # during the first case, before its completion callback.
                    checkpoint(output, result)

                    for backend in backends:
                        control.remaining()
                        ran_cases = run_cases(
                            bench,
                            runtime,
                            selected,
                            backend=backend,
                            vendor=args.vendor,
                            entry=entry,
                            identity=identities[backend],
                            output=output,
                            result=result,
                            cancel=cancel,
                            agent_factory=agent_factory,
                        )
                        # Skip an empty resumed campaign; retain transition and final checks.
                        if ran_cases or backend == backends[-1]:
                            entry.setdefault("end_health", {})[backend] = require_full_health(runtime, cancel)
                    if cancel.is_set():
                        raise RuntimeError("Acceptance cancelled")
                    entry["storage_end"] = storage_snapshot()
                    entry["agent_execution_failures"] = sorted(
                        {
                            summary["scenario_id"]
                            for record in entry["agents"].values()
                            for case in record["cases"]
                            for summary in case["scenario_summaries"]
                            if is_recovered_case_failure(summary) and summary["failure_stage"] == "agent"
                        }
                    )
                    entry["invalid_cases"] = sorted(
                        {
                            summary["scenario_id"]
                            for record in entry["agents"].values()
                            for case in record["cases"]
                            for summary in case["scenario_summaries"]
                            if summary.get("case_valid") is False
                        }
                    )
                    if entry["invalid_cases"]:
                        entry["status"] = "completed_with_case_failures"
                    elif entry["agent_execution_failures"]:
                        entry["status"] = "completed_with_agent_failures"
                    else:
                        entry["status"] = "passed"
                    if scale == "xs" and needs_smoke:
                        result["codex_xs_smoke"] = True
                    completed = True
                except BaseException as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    quarantine_partial_deployment(runtime, entry, output)
                    entry["status"] = "quarantined" if runtime.metadata.get("quarantined") else "failed_retained"
                    raise
                finally:
                    monitor.watch(None, None, None)
                    runtime.operation_control = None
                    if entry["resource_samples"]:
                        sample = peak_sample(([sample] if sample else []) + entry["resource_samples"])
                        atomic_write_json(output / "capacity-sample.json", sample)
                    if emergency.is_set():
                        entry["resource_emergency"] = "MemAvailable below 12 GiB"
                    checkpoint(output, result)
                    if runtime.state != "torn_down":
                        entry["runtime_disposition"] = release_runtime(
                            runtime,
                            owned=entry.get("runtime_owned", False),
                            completed=completed,
                            emergency=emergency.is_set(),
                        )
                    checkpoint(output, result)
            if any(entry.get("invalid_cases") for entry in result["scales"].values()):
                result["status"] = "completed_with_case_failures"
            elif any(entry.get("agent_execution_failures") for entry in result["scales"].values()):
                result["status"] = "completed_with_agent_failures"
            else:
                result["status"] = "passed"
        return 0
    except BaseException as exc:
        result["status"] = "failed_retained"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        monitor.close()
        if (
            emergency.is_set()
            and active_runtime is not None
            and active_entry is not None
            and active_runtime.state != "torn_down"
        ):
            try:
                active_entry["runtime_disposition"] = release_runtime(
                    active_runtime,
                    owned=active_entry.get("runtime_owned", False),
                    completed=False,
                    emergency=True,
                )
            except Exception as exc:
                active_entry["emergency_cleanup_error"] = str(exc)
        result["finished"] = datetime.now(UTC).isoformat()
        checkpoint(output, result)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
