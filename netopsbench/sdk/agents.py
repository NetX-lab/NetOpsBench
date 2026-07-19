"""Public SDK agent wrappers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from netopsbench.agents.base import DiagnosisResult, DiagnosticContext
from netopsbench.agents.handle import AgentHandle
from netopsbench.agents.tracing import AgentTraceRecorder


@runtime_checkable
class DiagnosticAgent(Protocol):
    async def diagnose(self, context: DiagnosticContext) -> DiagnosisResult: ...


@runtime_checkable
class SyncDiagnosticAgent(Protocol):
    def diagnose(self, context: DiagnosticContext) -> DiagnosisResult: ...


class AgentManager:
    """Minimal public agent manager.

    Tracks every :class:`AgentHandle` produced via :meth:`wrap` so that
    :meth:`netopsbench.sdk.core.NetOpsBench.close` can release agent-owned
    resources (LLM connections, MCP processes, etc.) in one call.
    """

    def __init__(self, platform: Any = None):
        self.platform = platform
        self.name = "agents"
        self._handles: list[AgentHandle] = []

    def wrap(self, agent: Any, name: str | None = None) -> AgentHandle:
        handle = AgentHandle(agent=agent, name=name)
        self._handles.append(handle)
        return handle

    def close(self) -> None:
        """Close every wrapped agent. Safe to call multiple times."""
        for handle in list(self._handles):
            handle.close()
        self._handles.clear()


__all__ = [
    "DiagnosisResult",
    "DiagnosticAgent",
    "SyncDiagnosticAgent",
    "AgentHandle",
    "AgentManager",
    "AgentTraceRecorder",
    "DiagnosticContext",
]
