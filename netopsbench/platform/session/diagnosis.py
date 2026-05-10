"""Diagnostic callback helpers for runtime-backed session execution."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from netopsbench.agents.base import DiagnosticContext


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class AgentHandleAdapter:
    """Lightweight async wrapper for agents without a stable ``diagnose`` protocol."""

    agent: Any
    name: str = "agent"

    def __init__(self, agent: Any):
        self.agent = agent
        derived_name = getattr(agent, "name", None)
        if isinstance(derived_name, str) and derived_name.strip():
            self.name = derived_name.strip()
        else:
            self.name = getattr(agent, "__class__", type(agent)).__name__

    async def diagnose(self, context: DiagnosticContext):
        diagnose_method = getattr(self.agent, "diagnose", None)
        if not callable(diagnose_method):
            raise AttributeError(f"{self.agent.__class__.__name__} must define diagnose()")
        return await _maybe_await(diagnose_method(context))


def run_agent_diagnose(handle: Any, context: DiagnosticContext):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(handle.diagnose(context))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(handle.diagnose(context)))
        return future.result()
