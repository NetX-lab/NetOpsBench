"""Public NetOpsBench SDK root."""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from netopsbench.logging_utils import configure_logging

from .agents import AgentManager
from .artifacts import ArtifactManager
from .evaluators import EvaluatorManager
from .faults import FaultManager
from .runtimes import RuntimeManager
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
        defaults: Mapping[str, object] | None = None,
        env: Mapping[str, str] | None = None,
        auto_load_env: bool = True,
    ):
        configure_logging()
        self.workspace = Path(workspace)
        self.defaults = dict(defaults or {})
        self.auto_load_env = auto_load_env
        if env is None and auto_load_env:
            self.env = dict(os.environ)
        else:
            self.env = dict(env or {})
        self._closed = False

        self.scenarios = self._bind_manager(ScenarioManager(workspace=self.workspace), "scenarios")
        self.agents = self._bind_manager(AgentManager(platform=self), "agents")
        self.faults = self._bind_manager(FaultManager(workspace=str(self.workspace)), "faults")
        self.runtimes = self._bind_manager(RuntimeManager(workspace=str(self.workspace)), "runtimes")
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
