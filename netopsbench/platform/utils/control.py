"""Cancellation and elapsed-time budgets for internal runtime operations."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from typing import overload

_operation_control = ContextVar("operation_control", default=None)


async def finish_cleanup(awaitable):
    """Drain owned cleanup despite repeated cancellation; report deferred cancellation."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def current_operation_control():
    return _operation_control.get()


@contextmanager
def operation_scope(control):
    previous = current_operation_control()
    if previous is not None:
        deadlines = [v for v in (previous.deadline, control.deadline) if v is not None]
        control = OperationControl(control.cancel_event or previous.cancel_event, min(deadlines) if deadlines else None)
    token = _operation_control.set(control)
    try:
        control.remaining()
        yield control
    finally:
        _operation_control.reset(token)


class OperationCancelled(RuntimeError):
    """The caller requested cancellation before the next side effect."""


@dataclass(frozen=True)
class OperationControl:
    cancel_event: Event | None = None
    deadline: float | None = None

    @overload
    def remaining(self, maximum: float) -> float: ...

    @overload
    def remaining(self, maximum: None = None) -> float | None: ...

    def remaining(self, maximum: float | None = None) -> float | None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OperationCancelled("Operation cancelled")
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Operation deadline exceeded")
        return remaining if maximum is None else min(remaining, maximum)

    def sleep(self, seconds: float) -> None:
        duration = self.remaining(seconds)
        if self.cancel_event is None:
            time.sleep(duration)
        else:
            self.cancel_event.wait(duration)
        self.remaining()
