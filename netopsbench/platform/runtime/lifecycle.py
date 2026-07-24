"""Structured, idempotent runtime lifecycle orchestration."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from netopsbench.config import config
from netopsbench.models.profiles import ScaleRegistry, default_scale_registry, get_scale_profile
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.client_agent.deploy import deploy_client_agents
from netopsbench.platform.observability.lifecycle import ensure_worker_observability
from netopsbench.platform.observability.ownership import ManagedBucketRegistry
from netopsbench.platform.runtime.deployment import (
    allocate_management_subnets,
    deploy_worker_lab,
    runtime_deploy_lock,
    teardown_worker_lab,
)
from netopsbench.platform.runtime.health import check_worker_health

logger = logging.getLogger(__name__)


class RuntimePoolLike(Protocol):
    id: str
    scale: str
    root_dir: Path
    telemetry_ownership_file: Path
    workers: list[RuntimeIdentity]
    _provision_created_buckets: list[str]

    @property
    def size(self) -> int: ...


class LifecycleStageResult(BaseModel):
    """Persisted result for one runtime lifecycle stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    status: Literal["completed", "failed"]
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RuntimeLifecycleError(RuntimeError):
    """Raised when a runtime lifecycle stage fails."""

    def __init__(self, result: LifecycleStageResult):
        self.result = result
        super().__init__(f"Runtime lifecycle stage {result.stage!r} failed: {result.error}")


def _parallel_job_count(scale: str, total: int, registry: ScaleRegistry | None = None) -> int:
    configured = get_scale_profile(scale, registry).worker_deploy_parallelism
    return max(1, min(total, configured))


def _append_worker_log_header(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n=== {label} ===\n")


def _worker_deploy_log_path(worker: RuntimeIdentity, runtime_root: Path | None = None) -> Path:
    if runtime_root is None:
        return worker.topology_dir / "deploy.log"
    return runtime_root / "logs" / f"worker_{worker.worker_index:02d}.deploy.log"


def deploy_workers(
    workers: Sequence[RuntimeIdentity],
    scale: str,
    runtime_root: Path | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> None:
    if not workers:
        return
    job_count = _parallel_job_count(scale, len(workers), scale_registry)

    def deploy(worker: RuntimeIdentity) -> None:
        logger.info(
            "[Worker Deploy %s/%s] %s subnet=%s",
            worker.worker_index,
            len(workers),
            worker.lab_name,
            worker.mgmt_subnet,
        )
        _append_worker_log_header(_worker_deploy_log_path(worker, runtime_root), f"worker deploy {worker.lab_name}")
        deploy_worker_lab(worker, scale, scale_registry)

    if job_count == 1:
        for worker in workers:
            deploy(worker)
        return

    failures: list[tuple[RuntimeIdentity, Exception]] = []
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        future_map = {executor.submit(deploy, worker): worker for worker in workers}
        for future in as_completed(future_map):
            try:
                future.result()
            except Exception as exc:
                failures.append((future_map[future], exc))
    if failures:
        worker, error = failures[0]
        logger.error(
            "Worker deployment failed for %s; see %s",
            worker.lab_name,
            _worker_deploy_log_path(worker, runtime_root),
        )
        raise error


def ensure_worker_client_agent(worker: RuntimeIdentity) -> None:
    deploy_client_agents(
        topology_dir=str(worker.topology_dir),
        influxdb_token=config.influxdb_token,
        influxdb_org=config.influxdb_org,
        influxdb_bucket=worker.bucket,
    )


def validate_worker_health(
    worker: RuntimeIdentity,
    runtime_root: Path | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> None:
    log_path = _worker_deploy_log_path(worker, runtime_root)
    _append_worker_log_header(log_path, "worker health validation")
    errors = check_worker_health(worker, scale_registry=scale_registry)
    if errors:
        message = "; ".join(errors)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"Health check errors: {message}\n")
        raise RuntimeError(f"Worker health check failed: {message}")


def deploy_worker_transactionally(
    worker: RuntimeIdentity,
    scale: str,
    scale_registry: ScaleRegistry | None = None,
) -> None:
    """Deploy one standalone worker with the same compensated lifecycle as pools."""
    registry = scale_registry or default_scale_registry()
    try:
        deploy_worker_lab(worker, scale, registry)
        ensure_worker_observability(worker)
        ensure_worker_client_agent(worker)
        validate_worker_health(worker, scale_registry=registry)
    except Exception as provision_error:
        try:
            teardown_worker_lab(worker, registry)
        except Exception as cleanup_error:
            raise RuntimeError(
                f"Worker provisioning failed ({type(provision_error).__name__}: {provision_error}) "
                f"and cleanup failed ({type(cleanup_error).__name__}: {cleanup_error})"
            ) from provision_error
        raise


def teardown_workers(workers: Sequence[RuntimeIdentity], scale_registry: ScaleRegistry | None = None) -> None:
    failures: list[str] = []
    for worker in workers:
        try:
            teardown_worker_lab(worker, scale_registry)
        except Exception as exc:
            logger.warning("worker teardown failed for %s", worker.lab_name, exc_info=True)
            failures.append(f"{worker.lab_name}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError("Worker teardown failed: " + "; ".join(failures))


class RuntimeLifecycle:
    """Execute the fixed runtime lifecycle stages."""

    def __init__(self, scale_registry: ScaleRegistry | None = None):
        self.scale_registry = scale_registry or default_scale_registry()

    def run(self, stage: str, runtime: RuntimePoolLike) -> LifecycleStageResult:
        operations = {
            "deploy": self._deploy,
            "observability": self._ensure_observability,
            "pingmesh": self._ensure_pingmesh,
            "warm": self._warm,
            "teardown": self._teardown,
        }
        try:
            operation = operations[stage]
        except KeyError as exc:
            raise ValueError(f"Unknown runtime lifecycle stage: {stage}") from exc

        started_at = datetime.now(UTC)
        started_tick = monotonic()
        try:
            details = operation(runtime) or {}
        except Exception as exc:
            ended_at = datetime.now(UTC)
            result = LifecycleStageResult(
                stage=stage,
                status="failed",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=monotonic() - started_tick,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise RuntimeLifecycleError(result) from exc

        return LifecycleStageResult(
            stage=stage,
            status="completed",
            started_at=started_at,
            ended_at=datetime.now(UTC),
            duration_seconds=monotonic() - started_tick,
            details=details,
        )

    def _deploy(self, runtime: RuntimePoolLike) -> dict[str, Any]:
        with runtime_deploy_lock():
            subnets = allocate_management_subnets(runtime.scale, runtime.size, self.scale_registry)
            runtime.workers = [
                worker.model_copy(update={"mgmt_subnet": subnets[index]})
                for index, worker in enumerate(runtime.workers)
            ]
            deploy_workers(runtime.workers, runtime.scale, runtime.root_dir, self.scale_registry)
        return {"workers": runtime.size}

    @staticmethod
    def _ensure_observability(runtime: RuntimePoolLike) -> dict[str, Any]:
        registry = ManagedBucketRegistry(runtime.telemetry_ownership_file)
        created: list[str] = []

        def record_created(bucket: str) -> None:
            registry.record_created(bucket, runtime.id)
            created.append(bucket)
            runtime._provision_created_buckets.append(bucket)

        for worker in runtime.workers:
            ensure_worker_observability(
                worker,
                on_bucket_created=record_created,
            )
        return {"workers": runtime.size, "created_buckets": created}

    @staticmethod
    def _ensure_pingmesh(runtime: RuntimePoolLike) -> dict[str, Any]:
        for worker in runtime.workers:
            ensure_worker_client_agent(worker)
        return {"workers": runtime.size}

    def _warm(self, runtime: RuntimePoolLike) -> dict[str, Any]:
        for worker in runtime.workers:
            validate_worker_health(worker, runtime.root_dir, self.scale_registry)
        return {"workers": runtime.size, "health": "ready"}

    def _teardown(self, runtime: RuntimePoolLike) -> dict[str, Any]:
        teardown_workers(runtime.workers, self.scale_registry)
        return {"workers": runtime.size}


__all__ = [
    "LifecycleStageResult",
    "RuntimeLifecycle",
    "RuntimeLifecycleError",
    "deploy_worker_transactionally",
    "deploy_workers",
    "ensure_worker_observability",
    "ensure_worker_client_agent",
    "teardown_workers",
    "validate_worker_health",
]
