"""Worker deployment, reuse, and teardown helpers."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from netopsbench.config import config
from netopsbench.platform.utils.events import emit as _emit
from netopsbench.platform.utils.proc import safe_run
from netopsbench.platform.worker.pool import WorkerSpec

logger = logging.getLogger(__name__)


def _parallel_job_count(env_var: str, total: int, default: int | None = None) -> int:
    resolved_default = total if default is None else default
    raw_value = str(os.environ.get(env_var, "")).strip()
    if raw_value:
        try:
            resolved_default = int(raw_value)
        except ValueError:
            resolved_default = default or total
    return max(1, min(total, resolved_default))


def _build_worker_env(worker: WorkerSpec, repo_root: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NETOPSBENCH_TOPOLOGY_DIR": worker.topology_dir,
            "NETOPSBENCH_TOPOLOGY_ID": Path(worker.topology_dir).name,
            "NETOPSBENCH_INFLUXDB_URL": env.get("NETOPSBENCH_INFLUXDB_URL", config.influxdb_url),
            "NETOPSBENCH_INFLUXDB_TOKEN": env.get("NETOPSBENCH_INFLUXDB_TOKEN", config.influxdb_token),
            "NETOPSBENCH_INFLUXDB_ORG": env.get("NETOPSBENCH_INFLUXDB_ORG", config.influxdb_org),
            "NETOPSBENCH_INFLUXDB_BUCKET": worker.bucket,
            "NETOPSBENCH_RUN_ID_SUFFIX": f"worker_{worker.index:02d}",
        }
    )
    if repo_root:
        existing_pythonpath = str(env.get("PYTHONPATH", "")).strip()
        pythonpath_parts = [repo_root]
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    worker_timeout = str(env.get("NETOPSBENCH_WORKER_AGENT_TIMEOUT_SECONDS", "")).strip()
    if worker_timeout:
        env["NETOPSBENCH_AGENT_TIMEOUT_SECONDS"] = worker_timeout
    else:
        env.setdefault("NETOPSBENCH_AGENT_TIMEOUT_SECONDS", "600")
    disable_langsmith = str(env.get("NETOPSBENCH_WORKER_DISABLE_LANGSMITH", "")).strip().lower()
    if disable_langsmith in {"1", "true", "yes", "on"}:
        env["LANGSMITH_API_KEY"] = ""
        env["LANGSMITH_TRACING"] = "false"
        env["LANGCHAIN_TRACING_V2"] = "false"
    return env


def _ensure_observability_core(repo_root: str) -> None:
    safe_run(
        ["bash", "scripts/observability/start_observability_core.sh"],
        cwd=repo_root,
        check=True,
        timeout=600,
    )


def _append_worker_log_header(path: str, label: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n=== {label} ===\n")


def deploy_workers(
    workers: Sequence[WorkerSpec], scale: str, repo_root: str, observability_core_ready: bool = False
) -> None:
    if not workers:
        return
    if not observability_core_ready:
        _ensure_observability_core(repo_root)
    job_count = _parallel_job_count("NETOPSBENCH_WORKER_DEPLOY_JOBS", len(workers))
    if job_count <= 1:
        for worker in workers:
            deploy_worker(worker, scale, repo_root, total_workers=len(workers), skip_observability_core_start=True)
        return
    _emit(f"Deploying {len(workers)} workers with {job_count} concurrent job(s)")
    failures: list[tuple[WorkerSpec, Exception]] = []
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        future_map = {
            executor.submit(deploy_worker, worker, scale, repo_root, len(workers), True): worker for worker in workers
        }
        for future in as_completed(future_map):
            worker = future_map[future]
            try:
                future.result()
            except Exception as exc:
                failures.append((worker, exc))
    if failures:
        worker, exc = failures[0]
        _emit(f"ERROR: Worker deployment failed for {worker.lab_name}; see {worker.deploy_log_path}")
        raise exc


def validate_worker_health(worker: WorkerSpec, repo_root: str) -> None:
    from netopsbench.platform.worker.health import check_worker_health

    _append_worker_log_header(worker.deploy_log_path, "worker health validation")
    errors = check_worker_health(worker.topology_dir)
    if errors:
        msg = "; ".join(errors)
        with open(worker.deploy_log_path, "a", encoding="utf-8") as log_fp:
            log_fp.write(f"Health check errors: {msg}\n")
        raise RuntimeError(f"Worker health check failed: {msg}")


def ensure_worker_pool_ready(workers: Sequence[WorkerSpec], scale: str, repo_root: str) -> dict[str, int]:
    if not workers:
        return {"reused": 0, "redeployed": 0}
    _ensure_observability_core(repo_root)
    job_count = _parallel_job_count("NETOPSBENCH_WORKER_REUSE_JOBS", len(workers))
    unhealthy: list[WorkerSpec] = []
    if job_count > 1:
        _emit(f"Revalidating {len(workers)} prepared workers with {job_count} concurrent job(s)")
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        future_map = {executor.submit(validate_worker_health, worker, repo_root): worker for worker in workers}
        for future in as_completed(future_map):
            worker = future_map[future]
            try:
                future.result()
                worker.reused_existing = True
                worker.redeployed = False
                _emit(f"[Worker Reuse Ready] {worker.lab_name}")
            except Exception:
                logger.warning("Worker %s health check failed; marking for redeploy", worker.lab_name, exc_info=True)
                worker.reused_existing = False
                unhealthy.append(worker)
    if not unhealthy:
        return {"reused": len(workers), "redeployed": 0}
    _emit(f"Worker pool health check failed for {len(unhealthy)} worker(s); redeploying only the unhealthy set")
    for worker in unhealthy:
        worker.redeployed = True
    deploy_workers(unhealthy, scale, repo_root, observability_core_ready=True)
    return {"reused": len(workers) - len(unhealthy), "redeployed": len(unhealthy)}


def deploy_worker(
    worker: WorkerSpec,
    scale: str,
    repo_root: str,
    total_workers: int | None = None,
    skip_observability_core_start: bool = False,
) -> None:
    total = total_workers or 1
    _emit(
        f"[Worker Deploy {worker.index}/{total}] {worker.lab_name} subnet={worker.mgmt_subnet} bucket={worker.bucket} scenarios={len(worker.scenarios)}"
    )
    env = _build_worker_env(worker, repo_root=repo_root)
    if skip_observability_core_start:
        env["NETOPSBENCH_SKIP_OBSERVABILITY_CORE_START"] = "1"
    _append_worker_log_header(worker.deploy_log_path, f"worker deploy {worker.lab_name}")
    try:
        with open(worker.deploy_log_path, "a", encoding="utf-8") as log_fp:
            safe_run(
                [
                    "bash",
                    "scripts/runtime/deploy_worker.sh",
                    scale,
                    worker.topology_dir,
                    worker.lab_name,
                    worker.mgmt_subnet,
                    worker.bucket,
                ],
                cwd=repo_root,
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=1800,
            )
    except subprocess.CalledProcessError:
        _emit(f"[Worker Deploy Failed] {worker.lab_name} (see {worker.deploy_log_path})")
        raise


def teardown_workers(workers: Sequence[WorkerSpec], repo_root: str) -> None:
    for worker in workers:
        teardown_worker(worker, repo_root)


def teardown_worker(worker: WorkerSpec, repo_root: str) -> None:
    try:
        safe_run(
            ["bash", "scripts/runtime/teardown_worker.sh", worker.topology_dir],
            cwd=repo_root,
            check=False,
            timeout=600,
        )
    except Exception:
        logger.warning("teardown_worker.sh failed for %s", worker.lab_name, exc_info=True)
        return
