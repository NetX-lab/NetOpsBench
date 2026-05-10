"""Public SDK agent wrappers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from netopsbench.sdk.types import DiagnosisResult, DiagnosticContext

logger = logging.getLogger(__name__)


@runtime_checkable
class DiagnosticAgent(Protocol):
    async def diagnose(self, context: DiagnosticContext) -> DiagnosisResult: ...


@runtime_checkable
class SyncDiagnosticAgent(Protocol):
    def diagnose(self, context: DiagnosticContext) -> DiagnosisResult: ...


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _derive_handle_name(agent: Any, name: str | None) -> str:
    if name:
        return name
    agent_name = getattr(agent, "name", None)
    if isinstance(agent_name, str) and agent_name.strip():
        return agent_name.strip()
    return getattr(agent, "__class__", type(agent)).__name__


def _run_async(coro: Any) -> Any:
    """Best-effort sync wrapper for an awaitable.

    Raises :class:`RuntimeError` if called from inside a running event loop —
    callers in async contexts should ``await`` the coroutine directly.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # We are inside a running loop; close the coroutine to avoid a
    # ``RuntimeWarning: coroutine was never awaited`` before raising.
    if inspect.iscoroutine(coro):
        coro.close()
    raise RuntimeError("Cannot call sync close() from a running event loop; " "await agent.aclose() directly instead.")


@dataclass
class AgentHandle:
    """Stable async wrapper around either sync or async diagnostic agents."""

    agent: Any
    name: str

    def __init__(self, agent: Any, name: str | None = None):
        self.agent = agent
        self.name = _derive_handle_name(agent, name)

    async def diagnose(self, context: DiagnosticContext) -> DiagnosisResult:
        diagnose_method = getattr(self.agent, "diagnose", None)
        if not callable(diagnose_method):
            raise AttributeError(f"{self.agent.__class__.__name__} must define diagnose()")
        result = await _maybe_await(diagnose_method(context))
        if not isinstance(result, DiagnosisResult):
            raise TypeError(f"Expected a DiagnosisResult, got {type(result).__name__}")
        return result

    def get_capabilities(self):
        capabilities = getattr(self.agent, "get_capabilities", None)
        if callable(capabilities):
            return capabilities()
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying agent if it exposes ``aclose()`` / ``close()``.

        Safe to call multiple times; safe to call on agents that do not
        define a close method.
        """
        aclose_method = getattr(self.agent, "aclose", None)
        if callable(aclose_method):
            await _maybe_await(aclose_method())
            return
        close_method = getattr(self.agent, "close", None)
        if callable(close_method):
            result = close_method()
            if inspect.isawaitable(result):
                await result

    def close(self) -> None:
        """Synchronous wrapper around :meth:`aclose`.

        Suitable for use from non-async script entry points (e.g. CLI ``main``).
        Logs and swallows errors so that ``bench.close()`` cleanup paths stay
        resilient.
        """
        try:
            _run_async(self.aclose())
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("AgentHandle.close() failed for %s", self.name, exc_info=True)


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
    "DiagnosticContext",
]
