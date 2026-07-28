"""Public runtime facades over the internal orchestration implementation."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from netopsbench.exceptions import RuntimeProvisionError
from netopsbench.models.profiles import ScaleRegistry
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime.manager import RuntimeManager as _PlatformRuntimeManager
from netopsbench.platform.runtime.manager import RuntimePool as _PlatformRuntimePool


class RuntimePool:
    """Stable SDK view of one platform runtime pool."""

    def __init__(self, runtime: _PlatformRuntimePool):
        self._runtime = runtime

    @property
    def id(self) -> str:
        return self._runtime.id

    @property
    def name(self) -> str:
        return self._runtime.name

    @property
    def scale(self) -> str:
        return self._runtime.scale

    @property
    def root_dir(self) -> Path:
        return self._runtime.root_dir

    @property
    def workers(self) -> list[RuntimeIdentity]:
        return self._runtime.workers

    @property
    def state(self) -> str:
        return self._runtime.state

    @property
    def metadata(self) -> dict[str, object]:
        return self._runtime.metadata

    @property
    def stage_results(self) -> dict[str, Any]:
        return self._runtime.stage_results

    @property
    def scale_registry(self) -> ScaleRegistry:
        return self._runtime.scale_registry

    @property
    def size(self) -> int:
        return self._runtime.size

    def deploy(self) -> RuntimePool:
        self._invoke(self._runtime.deploy)
        return self

    def ensure_observability(self) -> RuntimePool:
        self._invoke(self._runtime.ensure_observability)
        return self

    def ensure_pingmesh(self) -> RuntimePool:
        self._invoke(self._runtime.ensure_pingmesh)
        return self

    def warm(self) -> RuntimePool:
        self._invoke(self._runtime.warm)
        return self

    def teardown(self) -> RuntimePool:
        self._invoke(self._runtime.teardown)
        return self

    @staticmethod
    def _invoke(operation: Any) -> Any:
        try:
            return operation()
        except RuntimeProvisionError:
            raise
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc

    def status(self) -> dict[str, object]:
        return self._runtime.status()

    def describe(self) -> dict[str, object]:
        return self._runtime.describe()


class RuntimeManager:
    """SDK runtime manager that does not expose internal implementation types."""

    def __init__(
        self,
        workspace: str | Path = ".",
        scale_registry: ScaleRegistry | None = None,
    ):
        self._manager = _PlatformRuntimeManager(
            workspace=str(workspace),
            scale_registry=scale_registry,
        )

    @property
    def scale_registry(self) -> ScaleRegistry:
        return self._manager.scale_registry

    @property
    def runtime_root_dir(self) -> Path:
        return self._manager.runtime_root_dir

    def create(self, *, scale: str, workers: int = 1, name: str | None = None) -> RuntimePool:
        try:
            return RuntimePool(self._manager.create(scale=scale, workers=workers, name=name))
        except RuntimeProvisionError:
            raise
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc

    def provision(
        self,
        *,
        scale: str,
        workers: int = 1,
        name: str | None = None,
        root_dir: Path | None = None,
    ) -> RuntimePool:
        try:
            return RuntimePool(
                self._manager.provision(
                    scale=scale,
                    workers=workers,
                    name=name,
                    root_dir=root_dir,
                )
            )
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc

    def attach(self, root_dir: str | Path) -> RuntimePool:
        try:
            return RuntimePool(self._manager.attach(Path(root_dir)))
        except RuntimeProvisionError:
            raise
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc

    def list(self) -> builtins.list[RuntimePool]:
        try:
            return [RuntimePool(runtime) for runtime in self._manager.list()]
        except RuntimeProvisionError:
            raise
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc

    def get(self, name: str) -> RuntimePool | None:
        try:
            runtime = self._manager.get(name)
            return RuntimePool(runtime) if runtime is not None else None
        except RuntimeProvisionError:
            raise
        except Exception as exc:
            raise RuntimeProvisionError(str(exc)) from exc


__all__ = ["RuntimeIdentity", "RuntimeManager", "RuntimePool"]
