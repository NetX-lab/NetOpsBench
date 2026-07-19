"""Platform runtime pool adapters (internal)."""

from __future__ import annotations

import builtins
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, default_scale_registry
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.deployment import management_subnet
from netopsbench.platform.runtime.lifecycle import (
    LifecycleStageResult,
    RuntimeLifecycle,
    RuntimeLifecycleError,
    teardown_workers,
)
from netopsbench.platform.utils.proc import safe_run

logger = get_logger(__name__)


class RuntimeMetadataError(ValueError):
    """Raised when persisted runtime identity metadata is missing or invalid."""


@dataclass
class RuntimePool:
    id: str
    name: str
    scale: str
    root_dir: Path
    workers: list[RuntimeIdentity]
    state: str = "created"
    metadata: dict[str, object] = field(default_factory=dict)
    stage_results: dict[str, LifecycleStageResult] = field(default_factory=dict)
    scale_registry: ScaleRegistry = field(default_factory=default_scale_registry, repr=False)

    @property
    def size(self) -> int:
        return len(self.workers)

    def _payload(self) -> dict[str, object]:
        profile = self.scale_registry.get(self.scale)
        return {
            "schema_version": "3",
            "id": self.id,
            "name": self.name,
            "scale": self.scale,
            "scale_registry_sha256": self.scale_registry.digest,
            "resolved_scale_profile": profile.model_dump(mode="json"),
            "scale_profile_sha256": profile.digest,
            "state": self.state,
            "metadata": dict(self.metadata),
            "stage_results": {stage: result.model_dump(mode="json") for stage, result in self.stage_results.items()},
            "workers": [worker.model_dump(mode="json") for worker in self.workers],
        }

    def _write_metadata(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        (self.root_dir / "runtime.json").write_text(json.dumps(self._payload(), indent=2), encoding="utf-8")

    def _run_stage(self, stage: str, next_state: str) -> RuntimePool:
        previous = self.stage_results.get(stage)
        if previous is not None and previous.status == "completed":
            return self
        try:
            result = RuntimeLifecycle(scale_registry=self.scale_registry).run(stage, self)
        except RuntimeLifecycleError as exc:
            self.stage_results[stage] = exc.result
            self._write_metadata()
            raise
        self.stage_results[stage] = result
        self.state = next_state
        self._write_metadata()
        return self

    def deploy(self) -> RuntimePool:
        return self._run_stage("deploy", "deployed")

    def ensure_observability(self) -> RuntimePool:
        return self._run_stage("observability", "observability_ready")

    def ensure_pingmesh(self) -> RuntimePool:
        return self._run_stage("pingmesh", "pingmesh_ready")

    def warm(self) -> RuntimePool:
        return self._run_stage("warm", "warm")

    def status(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "scale": self.scale, "state": self.state}

    def teardown(self) -> RuntimePool:
        if self.state == "torn_down":
            return self
        try:
            RuntimeLifecycle(scale_registry=self.scale_registry).run("teardown", self)
        except RuntimeLifecycleError as exc:
            self.stage_results["teardown"] = exc.result
            self._write_metadata()
            raise
        self.state = "torn_down"
        metadata_path = self.root_dir / "runtime.json"
        if metadata_path.exists():
            metadata_path.unlink()
        if self.root_dir.exists():
            shutil.rmtree(self.root_dir, ignore_errors=True)
        return self


class RuntimeManager:
    def __init__(self, workspace: str = ".", scale_registry: ScaleRegistry | None = None):
        self.workspace = Path(workspace).expanduser().resolve()
        self.scale_registry = scale_registry or default_scale_registry()
        self.runtime_root_dir = self.workspace / ".netopsbench" / "runtimes"
        self.runtime_root_dir.mkdir(parents=True, exist_ok=True)

    def _build_runtime(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        self.scale_registry.get(scale)
        worker_count = max(1, int(workers))
        runtime_name = str(name or f"{scale}-{worker_count}-{uuid.uuid4().hex[:8]}").strip()
        runtime_root = Path(root_dir) if root_dir is not None else (self.runtime_root_dir / runtime_name)
        runtime_root.mkdir(parents=True, exist_ok=True)
        logs_dir = runtime_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        worker_items: list[RuntimeIdentity] = []
        for idx in range(1, worker_count + 1):
            worker_name = f"worker-{idx}"
            worker_dir = runtime_root / worker_name
            worker_dir.mkdir(exist_ok=True)
            lab_name = runtime_name if worker_count == 1 else f"{runtime_name}-w{idx:02d}"
            mgmt_subnet = management_subnet(scale, idx, self.scale_registry)
            identity = RuntimeIdentity.create(
                runtime_id=runtime_name,
                worker_id=worker_name,
                worker_index=idx,
                lab_name=lab_name,
                topology_dir=worker_dir,
                mgmt_subnet=mgmt_subnet,
                mgmt_network=f"clab-mgmt-{lab_name}",
            )
            worker_items.append(identity)
        runtime = RuntimePool(
            id=runtime_name,
            name=runtime_name,
            scale=scale,
            root_dir=runtime_root,
            workers=worker_items,
            scale_registry=self.scale_registry,
        )
        runtime._write_metadata()
        return runtime

    def provision(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        runtime = self._build_runtime(scale=scale, workers=workers, name=name, root_dir=root_dir)
        try:
            runtime.deploy().ensure_observability().ensure_pingmesh().warm()
        except Exception:
            logger.warning("Worker deployment failed; tearing down partial state", exc_info=True)
            # Best-effort teardown of partially-deployed workers (Docker
            # networks, containers, telegraf instances, etc.) before
            # cleaning up metadata on disk.
            try:
                teardown_workers(runtime.workers, self.scale_registry)
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
        runtime.state = "warm"
        runtime._write_metadata()
        return runtime

    def create(self, *, scale: str, workers: int = 1, name: str | None = None) -> RuntimePool:
        return self._build_runtime(scale=scale, workers=workers, name=name)

    def attach(self, root_dir: Path) -> RuntimePool:
        runtime_path = Path(root_dir)
        metadata_path = runtime_path / "runtime.json"
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeMetadataError(f"Unable to read runtime metadata {metadata_path}: {exc}") from exc
        if payload.get("schema_version") != "3":
            raise RuntimeMetadataError(
                "Unsupported runtime.json schema; recreate the runtime with the current RuntimeManager"
            )
        required_keys = {
            "schema_version",
            "id",
            "name",
            "scale",
            "scale_registry_sha256",
            "resolved_scale_profile",
            "scale_profile_sha256",
            "workers",
            "stage_results",
        }
        missing = sorted(required_keys - set(payload))
        if missing:
            raise RuntimeMetadataError(f"missing required runtime metadata: {', '.join(missing)}")
        profile = self.scale_registry.get(str(payload["scale"]))
        if payload["scale_profile_sha256"] != profile.digest:
            raise RuntimeMetadataError("Scale profile changed after runtime creation; recreate the runtime")
        if payload["resolved_scale_profile"] != profile.model_dump(mode="json"):
            raise RuntimeMetadataError("Persisted scale profile does not match its recorded identity")
        try:
            worker_items = [RuntimeIdentity.model_validate(item) for item in payload["workers"]]
            stage_results = {
                stage: LifecycleStageResult.model_validate(result)
                for stage, result in dict(payload.get("stage_results", {})).items()
            }
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise RuntimeMetadataError(
                f"Invalid schema-v3 runtime metadata in {metadata_path}; recreate the runtime: {exc}"
            ) from exc
        return RuntimePool(
            id=str(payload["id"]),
            name=str(payload["name"]),
            scale=str(payload["scale"]),
            root_dir=runtime_path,
            workers=worker_items,
            state=str(payload.get("state", "created")),
            metadata=dict(payload.get("metadata", {})),
            stage_results=stage_results,
            scale_registry=self.scale_registry,
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


__all__ = ["RuntimeManager", "RuntimeMetadataError", "RuntimePool"]
