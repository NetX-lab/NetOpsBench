"""Platform runtime pool adapters (internal)."""

from __future__ import annotations

import builtins
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.models.profiles import ScaleRegistry, default_scale_registry
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.observability.influxdb import delete_bucket
from netopsbench.platform.observability.ownership import ManagedBucketRegistry
from netopsbench.platform.runtime.deployment import management_subnet
from netopsbench.platform.runtime.lifecycle import (
    LifecycleStageResult,
    RuntimeLifecycle,
    RuntimeLifecycleError,
    teardown_workers,
)
from netopsbench.platform.utils.files import atomic_write_json
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
    telemetry_ownership_file: Path = field(repr=False, default=Path(".netopsbench/telemetry-buckets.json"))
    _provision_created_buckets: list[str] = field(default_factory=list, repr=False)

    @property
    def size(self) -> int:
        return len(self.workers)

    def describe(self) -> dict[str, object]:
        """Return the canonical serializable runtime description."""
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
        atomic_write_json(self.root_dir / "runtime.json", self.describe())

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
        # ``create()`` only reserves metadata; it has no physical lab to
        # teardown. A failed deploy does have a stage result and must still
        # attempt precise cleanup of any partially-created resources.
        if self.state != "created" or "deploy" in self.stage_results:
            try:
                RuntimeLifecycle(scale_registry=self.scale_registry).run("teardown", self)
            except RuntimeLifecycleError as exc:
                self.stage_results["teardown"] = exc.result
                self.state = "cleanup_failed"
                self.metadata["quarantined"] = True
                self.metadata["cleanup_error"] = exc.result.error or str(exc)
                self.metadata["cleanup_pending_buckets"] = ManagedBucketRegistry(
                    self.telemetry_ownership_file
                ).active_for_runtime(self.id)
                self._write_metadata()
                raise
            registry = ManagedBucketRegistry(self.telemetry_ownership_file)
            pending = self.metadata.get("cleanup_pending_buckets")
            if self.state == "cleanup_failed" and isinstance(pending, list):
                self._provision_created_buckets = [str(bucket) for bucket in pending]
            else:
                self._provision_created_buckets = registry.active_for_runtime(self.id)
            try:
                self._delete_provision_created_buckets()
            except Exception as exc:
                self.state = "cleanup_failed"
                self.metadata["quarantined"] = True
                self.metadata["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                self.metadata["cleanup_pending_buckets"] = list(self._provision_created_buckets)
                self._write_metadata()
                raise
            self.metadata.pop("cleanup_pending_buckets", None)

        if self.root_dir.exists():
            shutil.rmtree(self.root_dir, ignore_errors=True)
        if self.root_dir.exists():
            safe_run(
                ["sudo", "-n", "rm", "-rf", str(self.root_dir)],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        if self.root_dir.exists():
            self.state = "cleanup_failed"
            self.metadata["quarantined"] = True
            self.metadata["cleanup_error"] = f"runtime directory remained after teardown: {self.root_dir}"
            self._write_metadata()
            raise RuntimeError(str(self.metadata["cleanup_error"]))
        self.state = "torn_down"
        return self

    def _delete_provision_created_buckets(self) -> None:
        registry = ManagedBucketRegistry(self.telemetry_ownership_file)
        failed: list[str] = []
        errors: list[str] = []
        for bucket in self._provision_created_buckets:
            try:
                delete_bucket(config.influxdb_url, config.influxdb_token, bucket)
            except Exception as exc:  # noqa: BLE001 - retain exact cleanup failures for retry
                failed.append(bucket)
                errors.append(f"{bucket}: {type(exc).__name__}: {exc}")
            else:
                registry.mark_deleted(bucket)
        self._provision_created_buckets = failed
        if errors:
            raise RuntimeError("Managed bucket cleanup failed: " + "; ".join(errors))


class RuntimeManager:
    def __init__(self, workspace: str = ".", scale_registry: ScaleRegistry | None = None):
        self.workspace = Path(workspace).expanduser().resolve()
        self.scale_registry = scale_registry or default_scale_registry()
        self.runtime_root_dir = self.workspace / ".netopsbench" / "runtimes"
        self.telemetry_ownership_file = self.workspace / ".netopsbench" / "telemetry-buckets.json"
        self.runtime_root_dir.mkdir(parents=True, exist_ok=True)

    def _build_runtime(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        self.scale_registry.get(scale)
        worker_count = max(1, int(workers))
        runtime_name = str(name or f"{scale}-{worker_count}-{uuid.uuid4().hex[:8]}").strip()
        runtime_root = Path(root_dir) if root_dir is not None else (self.runtime_root_dir / runtime_name)
        runtime_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            runtime_root.mkdir()
        except FileExistsError as exc:
            raise RuntimeMetadataError(
                f"Runtime path already exists; attach or teardown it before reusing the name: {runtime_root}"
            ) from exc
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
            telemetry_ownership_file=self.telemetry_ownership_file,
        )
        runtime._write_metadata()
        return runtime

    def provision(
        self, *, scale: str, workers: int = 1, name: str | None = None, root_dir: Path | None = None
    ) -> RuntimePool:
        runtime = self._build_runtime(scale=scale, workers=workers, name=name, root_dir=root_dir)
        try:
            runtime.deploy().ensure_observability().ensure_pingmesh().warm()
        except Exception as provision_error:
            logger.warning("Worker deployment failed; tearing down partial state", exc_info=True)
            try:
                if runtime.metadata.get("deployment_started"):
                    teardown_workers(runtime.workers, self.scale_registry)
            except Exception as cleanup_error:
                logger.warning("Best-effort teardown_workers failed during cleanup", exc_info=True)
                runtime.state = "cleanup_failed"
                runtime.metadata["quarantined"] = True
                runtime.metadata["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                runtime.metadata["cleanup_pending_buckets"] = list(runtime._provision_created_buckets)
                runtime._write_metadata()
                raise RuntimeError(
                    f"Runtime provisioning failed ({type(provision_error).__name__}: "
                    f"{provision_error}) and cleanup failed "
                    f"({type(cleanup_error).__name__}: {cleanup_error})"
                ) from cleanup_error

            try:
                runtime._delete_provision_created_buckets()
            except Exception as cleanup_error:
                logger.warning("Managed bucket cleanup failed during provisioning compensation", exc_info=True)
                runtime.state = "cleanup_failed"
                runtime.metadata["quarantined"] = True
                runtime.metadata["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                runtime.metadata["cleanup_pending_buckets"] = list(runtime._provision_created_buckets)
                runtime._write_metadata()
                raise RuntimeError(
                    f"Runtime provisioning failed ({type(provision_error).__name__}: "
                    f"{provision_error}) and cleanup failed "
                    f"({type(cleanup_error).__name__}: {cleanup_error})"
                ) from cleanup_error
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
            if runtime.root_dir.exists():
                residual_cleanup_error = RuntimeError(
                    f"runtime directory remained after provisioning cleanup: {runtime.root_dir}"
                )
                runtime.state = "cleanup_failed"
                runtime.metadata["quarantined"] = True
                runtime.metadata["cleanup_error"] = str(residual_cleanup_error)
                try:
                    runtime._write_metadata()
                except Exception:
                    logger.warning("Unable to persist provisioning cleanup failure", exc_info=True)
                raise RuntimeError(
                    f"Runtime provisioning failed ({type(provision_error).__name__}: "
                    f"{provision_error}) and cleanup failed ({residual_cleanup_error})"
                ) from residual_cleanup_error
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
            telemetry_ownership_file=self.telemetry_ownership_file,
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
