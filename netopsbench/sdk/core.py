"""Public NetOpsBench SDK root."""

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
        if self._closed:
            return
        self._closed = True
        if self._simulators is not None:
            try:
                self._simulators.close()
            except Exception:
                logger.warning("SimulatorManager.close() failed", exc_info=True)
        agents_close = getattr(self.agents, "close", None)
        if callable(agents_close):
            try:
                agents_close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning("AgentManager.close() failed", exc_info=True)

    def __enter__(self) -> "NetOpsBench":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
