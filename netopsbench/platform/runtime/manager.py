"""Platform runtime pool adapters (internal)."""

from __future__ import annotations

import builtins
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from netopsbench.config import repo_root
from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.proc import safe_run, sudo_prefix
from netopsbench.platform.worker.lifecycle import deploy_workers, teardown_workers
from netopsbench.platform.worker.pool import WorkerSpec, _worker_bucket, _worker_mgmt_subnet

logger = get_logger(__name__)


def _runtime_subnet_base(scale: str, runtime_name: str) -> int:
    base = _worker_mgmt_subnet(scale, 1).split(".")[2]
    try:
        base_value = int(base) - 1
    except ValueError:
        base_value = 100
    match = re.search(r"(\d+)(?!.*\d)", runtime_name or "")
    if match:
        offset = int(match.group(1)) % 80
    else:
        offset = sum(ord(ch) for ch in (runtime_name or scale)) % 80
    return min(254, base_value + offset)


@dataclass
class RuntimePool:
    id: str
    name: str
    scale: str
    root_dir: Path
    workers: list[WorkerSpec]
    state: str = "created"
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.workers)

    def _payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "scale": self.scale,
            "state": self.state,
            "metadata": dict(self.metadata),
            "workers": [
                {
                    "id": worker.id,
                    "index": worker.index,
                    "name": worker.name,
                    "root_dir": str(worker.root_dir) if worker.root_dir is not None else None,
                    "lab_name": worker.lab_name,
                    "topology_dir": worker.topology_dir,
                    "mgmt_subnet": worker.mgmt_subnet,
                    "bucket": worker.bucket,
                    "shard_dir": worker.shard_dir,
                    "report_path": worker.report_path,
                    "log_path": worker.log_path,
                    "deploy_log_path": worker.deploy_log_path,
                    "metadata": dict(worker.metadata),
                }
                for worker in self.workers
            ],
        }

    def _write_metadata(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        (self.root_dir / "runtime.json").write_text(json.dumps(self._payload(), indent=2), encoding="utf-8")

    def deploy(self) -> RuntimePool:
        self.state = "deployed"
        self._write_metadata()
        return self

    def ensure_observability(self) -> RuntimePool:
        self.state = "observability_ready"
        self._write_metadata()
        return self

    def ensure_pingmesh(self) -> RuntimePool:
        self.state = "pingmesh_ready"
        self._write_metadata()
        return self

    def warm(self) -> RuntimePool:
        self.state = "warm"
        self._write_metadata()
        return self

    def status(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "scale": self.scale, "state": self.state}

    def teardown(self) -> RuntimePool:
        if self.workers:
            teardown_workers(self.workers, str(repo_root()))
        self.state = "torn_down"
        metadata_path = self.root_dir / "runtime.json"
        if metadata_path.exists():
            metadata_path.unlink()
        if self.root_dir.exists():
            shutil.rmtree(self.root_dir, ignore_errors=True)
        return self


class RuntimeManager:
    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace)
        self.runtime_root_dir = self.workspace / ".netopsbench" / "runtimes"
        self.runtime_root_dir.mkdir(parents=True, exist_ok=True)

    def _build_runtime(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        worker_count = max(1, int(workers))
        runtime_name = str(name or f"{scale}-{worker_count}").strip()
        runtime_root = Path(root_dir) if root_dir is not None else (self.runtime_root_dir / runtime_name)
        subnet_base = _runtime_subnet_base(scale, runtime_name)
        runtime_root.mkdir(parents=True, exist_ok=True)
        logs_dir = runtime_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        worker_items: list[WorkerSpec] = []
        for idx in range(1, worker_count + 1):
            worker_name = f"worker-{idx}"
            worker_dir = runtime_root / worker_name
            worker_dir.mkdir(exist_ok=True)
            worker_items.append(
                WorkerSpec(
                    id=worker_name,
                    name=worker_name,
                    root_dir=worker_dir,
                    index=idx,
                    lab_name=(runtime_name if worker_count == 1 else f"{runtime_name}-w{idx:02d}"),
                    topology_dir=str(worker_dir),
                    mgmt_subnet=f"172.31.{min(254, subnet_base + idx)}.0/24",
                    bucket=_worker_bucket(scale, idx),
                    shard_dir=str(worker_dir / "scenarios"),
                    report_path=str(logs_dir / f"worker_{idx:02d}.report.json"),
                    log_path=str(logs_dir / f"worker_{idx:02d}.log"),
                    deploy_log_path=str(logs_dir / f"worker_{idx:02d}.deploy.log"),
                )
            )
        runtime = RuntimePool(
            id=runtime_name, name=runtime_name, scale=scale, root_dir=runtime_root, workers=worker_items
        )
        runtime._write_metadata()
        return runtime

    def _used_management_subnets(self) -> set[str]:
        try:
            result = safe_run(
                [*sudo_prefix(), "docker", "network", "ls", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception:
            logger.warning("docker network ls failed; assuming no networks in use", exc_info=True)
            return set()
        used: set[str] = set()
        for network_id in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            inspect = safe_run(
                [
                    *sudo_prefix(),
                    "docker",
                    "network",
                    "inspect",
                    network_id,
                    "--format",
                    "{{range .IPAM.Config}}{{println .Subnet}}{{end}}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            for subnet in inspect.stdout.splitlines():
                subnet = subnet.strip()
                if subnet:
                    used.add(subnet)
        return used

    def _allocate_management_subnets(self, scale: str, worker_count: int) -> builtins.list[str]:
        used = self._used_management_subnets()
        start = int(_worker_mgmt_subnet(scale, 1).split(".")[2])
        selected: list[str] = []
        for octet in range(start, 255):
            subnet = f"172.31.{octet}.0/24"
            if subnet in used or subnet in selected:
                continue
            selected.append(subnet)
            if len(selected) == worker_count:
                return selected
        raise RuntimeError(f"unable to allocate {worker_count} unique management subnets for scale {scale}")

    def provision(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        runtime = self._build_runtime(scale=scale, workers=workers, name=name, root_dir=root_dir)
        try:
            allocated_subnets = self._allocate_management_subnets(scale, runtime.size)
            runtime.workers = [
                WorkerSpec(
                    id=worker.id,
                    name=worker.name,
                    root_dir=worker.root_dir,
                    index=worker.index,
                    lab_name=worker.lab_name,
                    topology_dir=worker.topology_dir,
                    mgmt_subnet=allocated_subnets[idx],
                    bucket=worker.bucket,
                    shard_dir=worker.shard_dir,
                    report_path=worker.report_path,
                    log_path=worker.log_path,
                    deploy_log_path=worker.deploy_log_path,
                    metadata=dict(worker.metadata),
                )
                for idx, worker in enumerate(runtime.workers)
            ]
            deploy_workers(runtime.workers, scale, str(repo_root()))
        except Exception:
            logger.warning("Worker deployment failed; tearing down partial state", exc_info=True)
            # Best-effort teardown of partially-deployed workers (Docker
            # networks, containers, telegraf instances, etc.) before
            # cleaning up metadata on disk.
            try:
                teardown_workers(runtime.workers, str(repo_root()))
            except Exception:
                logger.warning("Best-effort teardown_workers failed during cleanup", exc_info=True)
            # Use sudo rm to handle root-owned files left by containerlab.
            try:
                safe_run(
                    ["sudo", "-n", "rm", "-rf", str(runtime.root_dir)],
                    check=False,
                    capture_output=True,
                    timeout=120,
                )
            except Exception:
                logger.warning("sudo rm of runtime root failed during cleanup", exc_info=True)
            if runtime.root_dir.exists():
                shutil.rmtree(runtime.root_dir, ignore_errors=True)
            raise
        runtime.metadata["provisioning_mode"] = "worker_pool"
        runtime.state = "deployed"
        runtime._write_metadata()
        return runtime

    def create(self, *, scale: str, workers: int = 1, name: str | None = None) -> RuntimePool:
        return self._build_runtime(scale=scale, workers=workers, name=name)

    def attach(self, root_dir: Path) -> RuntimePool:
        runtime_path = Path(root_dir)
        payload = json.loads((runtime_path / "runtime.json").read_text(encoding="utf-8"))
        required_keys = {"id", "name", "scale", "workers"}
        missing = sorted(required_keys - set(payload))
        if missing:
            raise ValueError(f"missing required runtime metadata: {', '.join(missing)}")
        worker_items = [
            WorkerSpec(
                id=str(item.get("id") or item.get("name") or f"worker-{index+1}"),
                name=str(item.get("name") or item.get("id") or f"worker-{index+1}"),
                root_dir=Path(item["root_dir"]) if item.get("root_dir") else None,
                index=int(item.get("index", index + 1)),
                lab_name=item.get("lab_name") or str(item.get("name") or item.get("id") or f"worker-{index+1}"),
                topology_dir=item.get("topology_dir")
                or str(item.get("root_dir") or runtime_path / f"worker-{index+1}"),
                mgmt_subnet=item.get("mgmt_subnet") or _worker_mgmt_subnet(str(payload["scale"]), index + 1),
                bucket=item.get("bucket") or _worker_bucket(str(payload["scale"]), index + 1),
                shard_dir=item.get("shard_dir") or str(runtime_path / f"worker-{index+1}" / "scenarios"),
                report_path=item.get("report_path") or str(runtime_path / "logs" / f"worker_{index+1:02d}.report.json"),
                log_path=item.get("log_path") or str(runtime_path / "logs" / f"worker_{index+1:02d}.log"),
                deploy_log_path=item.get("deploy_log_path")
                or str(runtime_path / "logs" / f"worker_{index+1:02d}.deploy.log"),
                metadata=dict(item.get("metadata", {})),
            )
            for index, item in enumerate(payload["workers"])
        ]
        return RuntimePool(
            id=str(payload["id"]),
            name=str(payload["name"]),
            scale=str(payload["scale"]),
            root_dir=runtime_path,
            workers=worker_items,
            state=str(payload.get("state", "created")),
            metadata=dict(payload.get("metadata", {})),
        )

    def list(self) -> builtins.list[RuntimePool]:
        runtimes: list[RuntimePool] = []
        for candidate in sorted(self.runtime_root_dir.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir():
                continue
            metadata_path = candidate / "runtime.json"
            if not metadata_path.exists():
                continue
            runtimes.append(self.attach(candidate))
        return runtimes

    def get(self, name: str) -> RuntimePool | None:
        for runtime in self.list():
            if runtime.name == name:
                return runtime
        return None


__all__ = ["RuntimeManager", "RuntimePool"]
