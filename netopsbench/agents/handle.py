"""Shared sync/async diagnostic agent handle."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from netopsbench.agents.base import DiagnosisResult, DiagnosticContext

logger = logging.getLogger(__name__)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _derive_name(agent: Any, name: str | None) -> str:
    if name:
        return name
    agent_name = getattr(agent, "name", None)
    if isinstance(agent_name, str) and agent_name.strip():
        return agent_name.strip()
    return getattr(agent, "__class__", type(agent)).__name__


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if inspect.iscoroutine(coro):
        coro.close()
    raise RuntimeError("Cannot call sync close() from a running event loop; await agent.aclose() instead.")


class AgentHandle:
    """Async wrapper around either a synchronous or asynchronous agent."""

    agent: Any
    name: str

    def __init__(self, agent: Any, name: str | None = None):
        self.agent = agent
        self.name = _derive_name(agent, name)

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
        return capabilities() if callable(capabilities) else []

    async def aclose(self) -> None:
        aclose_method = getattr(self.agent, "aclose", None)
        if callable(aclose_method):
            await _maybe_await(aclose_method())
            return
        close_method = getattr(self.agent, "close", None)
        if callable(close_method):
            await _maybe_await(close_method())

    def close(self) -> None:
        try:
            _run_async(self.aclose())
        except Exception:
            logger.warning("AgentHandle.close() failed for %s", self.name, exc_info=True)


__all__ = ["AgentHandle"]
