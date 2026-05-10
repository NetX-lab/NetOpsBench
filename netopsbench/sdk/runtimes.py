"""Public runtime manager exports."""

from netopsbench.platform.runtime.manager import RuntimeManager as _RuntimeManager
from netopsbench.platform.runtime.manager import RuntimePool as _RuntimePool
from netopsbench.platform.worker.pool import WorkerSpec as _RuntimeWorker

RuntimeManager = _RuntimeManager
RuntimePool = _RuntimePool
RuntimeWorker = _RuntimeWorker

RuntimeManager.__module__ = __name__
RuntimePool.__module__ = __name__
RuntimeWorker.__module__ = __name__

__all__ = ["RuntimeManager", "RuntimePool", "RuntimeWorker"]
