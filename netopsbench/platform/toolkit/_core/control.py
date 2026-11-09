"""Propagate a diagnostic session's remaining time into existing tool transports."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from netopsbench.platform.utils.control import OperationControl

_current_control: ContextVar[OperationControl | None] = ContextVar("diagnostic_tool_control", default=None)
_current_budget = ContextVar("diagnosis_budget", default=None)


@dataclass
class _DiagnosisBudget:
    deadline: float
    timer: asyncio.Timeout
    parent: _DiagnosisBudget | None
    finished_at: float | None = None


@asynccontextmanager
async def diagnosis_budget_scope(seconds: float | None):
    """Share the earliest work deadline; required cleanup has separate timing."""
    previous = _current_budget.get()
    deadline = time.monotonic() + seconds if seconds is not None else None
    if previous is not None and (deadline is None or previous.deadline <= deadline):
        yield previous
        return
    if deadline is None:
        yield None
        return
    budget = _DiagnosisBudget(deadline, asyncio.timeout_at(deadline), previous)
    token = _current_budget.set(budget)
    try:
        with tool_deadline_scope(deadline):
            async with budget.timer:
                yield budget
                # Sync work can block the event loop past its deadline.
                if (budget.finished_at if budget.finished_at is not None else time.monotonic()) >= deadline:
                    raise TimeoutError("Total diagnosis timeout exceeded")
    finally:
        _current_budget.reset(token)


def finish_diagnosis_work():
    """Stop work timers when the adapter enters its final resource cleanup."""
    budget = _current_budget.get()
    now = time.monotonic()
    while budget is not None:
        if budget.finished_at is None:
            budget.finished_at = now
            if not budget.timer.expired():
                budget.timer.reschedule(None)
        budget = budget.parent


def current_tool_control() -> OperationControl | None:
    return _current_control.get()


@contextmanager
def tool_deadline_scope(deadline: float):
    previous = current_tool_control()
    control = OperationControl(
        deadline=min(previous.deadline, deadline) if previous and previous.deadline else deadline
    )
    token = _current_control.set(control)
    try:
        control.remaining()
        yield control
    finally:
        _current_control.reset(token)
