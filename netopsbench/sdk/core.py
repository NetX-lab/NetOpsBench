"""Public NetOpsBench SDK root."""

import asyncio
import inspect
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import configure_logging

from .agents import AgentManager
from .artifacts import ArtifactManager
from .evaluators import EvaluatorManager
from .faults import FaultManager
from .runtimes import RuntimeManager
from .scales import ScaleManager
from .scenarios import ScenarioManager
from .sessions import SessionManager

logger = logging.getLogger(__name__)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if inspect.iscoroutine(coro):
        coro.close()
    raise RuntimeError("Cannot call sync close() from a running event loop; await bench.aclose() instead.")


class NetOpsBench:
    """Root object for the public NetOpsBench SDK.

    Acts as a context manager so resources owned by sub-managers (wrapped
    agents, runtime pools, etc.) are released cleanly::

        with NetOpsBench(workspace=".") as bench:
            run = bench.sessions.run_scenario(scenario=..., agent=...)
            report = run.wait()

    Calling :meth:`close` is idempotent. Outside a ``with`` block users may
    invoke :meth:`close` directly when finished.
    """

    def __init__(
        self,
        workspace: str = ".",
        *,
        scale_profiles: Iterable[str | Path] = (),
    ):
        configure_logging()
        self.workspace = Path(workspace)
        self._closed = False
        self._simulators = None

        self.scales = self._bind_manager(ScaleManager(scale_profiles), "scales")
        self.scenarios = self._bind_manager(
            ScenarioManager(workspace=self.workspace, scale_registry=self.scales.registry),
            "scenarios",
        )
        self.agents = self._bind_manager(AgentManager(platform=self), "agents")
        self.faults = self._bind_manager(FaultManager(workspace=str(self.workspace)), "faults")
        self.runtimes = self._bind_manager(
            RuntimeManager(workspace=str(self.workspace), scale_registry=self.scales.registry),
            "runtimes",
        )
        self.artifacts = self._bind_manager(ArtifactManager(workspace=str(self.workspace)), "artifacts")
        self.evaluators = self._bind_manager(EvaluatorManager(), "evaluators")
        self.sessions = self._bind_manager(
            SessionManager(
                platform=self,
                workspace=str(self.workspace),
                runtime_manager=self.runtimes,
                artifact_manager=self.artifacts,
            ),
            "sessions",
        )

    @property
    def simulators(self):
        """Lazily construct the optional simulator manager."""
        if self._simulators is None:
            from .simulators import SimulatorManager

            self._simulators = self._bind_manager(
                SimulatorManager(
                    scale_registry=self.scales.registry,
                    runtime_manager=self.runtimes,
                    fault_registry=self.faults.spec_registry,
                ),
                "simulators",
            )
        return self._simulators

    def _bind_manager(self, manager: Any, name: str) -> Any:
        manager.platform = self
        manager.name = name
        return manager

    # ------------------------------------------------------------------
    # Lifecycle / context manager
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release resources owned by sub-managers.

        Currently closes every :class:`AgentHandle` produced via
        ``bench.agents.wrap(...)``. Idempotent: safe to call multiple times
        and from ``__del__``-like cleanup paths.
        """
        _run_async(self.aclose())

    async def aclose(self) -> None:
        """Release owned resources without blocking a running event loop.

        The root is marked closed only after all cleanup succeeds, so a failed
        close remains retryable.
        """
        if self._closed:
            return
        if self._simulators is not None:
            await _maybe_await(self._simulators.close())
        await self.agents.aclose()
        self._closed = True

    def __enter__(self) -> "NetOpsBench":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    async def __aenter__(self) -> "NetOpsBench":
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()
