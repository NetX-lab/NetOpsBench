"""Short-lived, single-consumer handoff of a worker's verified healthy window.

This stores window identity, never telemetry rows. Each consumer queries InfluxDB
again so late arrivals remain visible. Ownership ends with the suite's executor.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.platform.utils.proc import docker_prefix, safe_run

MAX_WINDOW_AGE_SECONDS = 120
RECALIBRATE_SECONDS = 600


@dataclass(frozen=True)
class RuntimeScope:
    identity: dict
    configs: dict[str, str]
    containers: dict[str, list[str]]

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


def scope_changes(before, after):
    if before == after:
        return []
    if not isinstance(before, RuntimeScope) or not isinstance(after, RuntimeScope):
        return ["scope_unavailable_or_changed"]
    changes = [
        f"identity:{key}"
        for key in before.identity.keys() | after.identity.keys()
        if before.identity.get(key) != after.identity.get(key)
    ]
    for group in ("configs", "containers"):
        old, new = getattr(before, group), getattr(after, group)
        changes.extend(f"{group}:{key}" for key in old.keys() | new.keys() if old.get(key) != new.get(key))
    return sorted(changes)


def normalized_frr(text):
    """Discard FRR display headers/comments, preserving command order and nesting."""
    if "Current configuration:\n" in text:
        text = text.split("Current configuration:\n", 1)[1]
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [
        line
        for line in lines
        if line.strip() and not line.lstrip().startswith("!") and line.strip() != "Building configuration..."
    ]
    if lines and lines[-1].strip() == "end":
        lines.pop()
    if not any(line.startswith("router bgp ") for line in lines):
        raise ValueError("FRR configuration is missing the BGP router")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def frr_snapshot(runner, device):
    container = runner.injector.container_names[device]
    result = safe_run(
        [*docker_prefix(), "docker", "exec", container, "vtysh", "-c", "show running-config"], check=True, timeout=30
    )
    path = Path(runner.topology_dir) / "configs" / "frr" / f"{device}.conf"
    saved = path.read_bytes()
    return {
        "device": device,
        "running": normalized_frr(result.stdout),
        "saved": normalized_frr(saved.decode()),
        "saved_bytes": hashlib.sha256(saved).hexdigest(),
    }


def restored_scope_errors(before, after, scenario, config_proof):
    """Permit only an owned, verified FRR rewrite and the requested device restart."""
    changes = scope_changes(before, after)
    allowed = set()
    device = scenario.episode.target_device
    if config_proof and config_proof.get("restored"):
        allowed.add(f"configs:configs/frr/{device}.conf")
    if scenario.episode.fault_type == "device_down" and isinstance(after, RuntimeScope):
        allowed.update(
            f"containers:{name}"
            for name in after.containers
            if name.endswith(f"-{device}")
            and isinstance(before, RuntimeScope)
            and name in before.containers
            and before.containers[name][1] == after.containers[name][1]
        )
    return [change for change in changes if change not in allowed]


@dataclass
class BaselineHandoff:
    window: dict | None = None
    scope: RuntimeScope | None = None
    source: str | None = None
    epoch: float | None = None
    last_changes: list[str] | None = None
    last_reason: str = "no_verified_window"
    last_verified_end: datetime | None = None
    uses: int = 0
    reusable: bool = False
    max_cases: int = 5
    max_age_seconds: int = MAX_WINDOW_AGE_SECONDS
    ready: bool = False

    def clear(self, reason: str = "no_verified_window") -> None:
        self.window = self.scope = self.source = self.epoch = None
        self.last_reason = reason
        self.uses = 0
        self.ready = False

    def fresh_epoch(self, window: dict | None = None) -> None:
        self.clear()
        self.epoch = time.monotonic()
        if window is not None:
            self.last_verified_end = datetime.fromisoformat(window["end_time"].replace("Z", "+00:00"))

    def take(self, scope: RuntimeScope | None, minimum_seconds: int) -> tuple[dict | None, str]:
        window, previous_scope = self.window, self.scope
        self.last_changes = scope_changes(previous_scope, scope)
        if window is None:
            return None, self.last_reason
        if not self.reusable:
            self.window = None  # Legacy windows have one consumer.
        elif not self.ready:
            return None, "previous_case_not_verified"
        elif self.uses >= self.max_cases:
            return None, "case_limit"
        self.ready = False  # Failure/interruption cannot silently reuse this window.
        if window is None:
            return None, self.last_reason
        self.last_reason = "window_already_consumed"
        if not scope:
            return None, "scope_unavailable"
        if scope != previous_scope:
            return None, "scope_changed"
        if self.epoch is None or (not self.reusable and not 0 <= time.monotonic() - self.epoch <= RECALIBRATE_SECONDS):
            return None, "recalibration_due"
        reason = window_error(window, minimum_seconds, max_age_seconds=self.max_age_seconds)
        if reason:
            return None, reason
        self.uses += 1
        return dict(window), "verified_previous_case"

    def publish(
        self, window: dict, scope: RuntimeScope | None, source: str, minimum_seconds: int, *, verified: bool = False
    ) -> bool:
        reason = (
            "scope_unavailable"
            if not scope
            else window_error(window, minimum_seconds, max_age_seconds=self.max_age_seconds)
        )
        if reason:
            self.window = None
            self.last_reason = reason
            return False
        end = datetime.fromisoformat(window["end_time"].replace("Z", "+00:00"))
        if verified and (self.last_verified_end is None or end > self.last_verified_end):
            # A new, independently verified full window is itself calibration.
            # Republishing a prior window cannot extend the calibration epoch.
            self.epoch = time.monotonic()
            self.last_verified_end = end
            self.uses = 0
        if self.epoch is None:
            self.window = None
            self.last_reason = "calibration_missing"
            return False
        self.window, self.scope, self.source = dict(window), scope, source
        self.ready = True
        self.last_reason = "verified_window_published" if verified else "window_published"
        return True


def window_error(window: dict, minimum_seconds: int, *, max_age_seconds: int = MAX_WINDOW_AGE_SECONDS) -> str | None:
    try:
        start = datetime.fromisoformat(window["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["end_time"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            return "invalid_window"
        if (end - start).total_seconds() < minimum_seconds or int(window["duration_seconds"]) < minimum_seconds:
            return "short_window"
        age = (datetime.now(UTC) - end).total_seconds()
        if age < 0:
            return "future_window"
        if age > max_age_seconds:
            return "expired_window"
    except (KeyError, TypeError, ValueError, OverflowError):
        return "invalid_window"
    return None


def valid_window(window: dict, minimum_seconds: int) -> bool:
    return window_error(window, minimum_seconds) is None


def runtime_scope(runner, scenario) -> RuntimeScope | None:
    """Require unchanged runtime instances, generated configs and traffic plans."""
    worker = runner.runtime_worker
    controller = runner.traffic_controller
    if worker is None or controller is None or not controller.generation or not controller.plan_digests:
        return None
    root = Path(runner.topology_dir)
    paths = [root / "topology.json", *sorted((root / "configs").rglob("*"))]
    configs = {}
    for path in paths:
        if path.is_file():
            configs[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = runner.topology_metadata
    names = sorted(f"clab-{worker.lab_name}-{device['name']}" for device in manifest["devices"])
    inspected = safe_run(
        [
            *docker_prefix(),
            "docker",
            "inspect",
            "--format",
            "{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Pid}} {{.State.Running}} {{.State.OOMKilled}}",
            *names,
        ],
        check=True,
        timeout=30,
    )
    rows = [line.split() for line in inspected.stdout.splitlines() if line.strip()]
    if len(rows) != len(names) or any(len(row) != 6 or row[4:] != ["true", "false"] for row in rows):
        return None
    containers = dict(zip(names, rows, strict=True))
    client_names = {device["name"] for device in manifest["devices"] if device["role"] == "client"}
    starts = [
        row[2] for name, row in containers.items() if name.removeprefix(f"clab-{worker.lab_name}-") in client_names
    ]
    if starts:
        runner._pingmesh_runtime_start = min(starts)
    return RuntimeScope(
        identity={
            "runtime": worker.runtime_id,
            "worker": worker.worker_id,
            "topology": runner.topology_id,
            "pingmesh_plan_id": runner.topology_metadata["pingmesh_plan"]["settings"]["plan_id"],
            "bucket": runner.influxdb_bucket,
            "scale": runner.scale_registry.get(scenario.scale).digest,
            "traffic_profile": scenario.traffic_profile,
            "generation": controller.generation,
            "plans": dict(controller.plan_digests),
        },
        configs=configs,
        containers=containers,
    )
