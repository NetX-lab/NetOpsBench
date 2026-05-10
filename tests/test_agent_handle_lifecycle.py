"""Tests for :class:`AgentHandle` / :class:`AgentManager` lifecycle hooks."""

from __future__ import annotations

import asyncio

from netopsbench.sdk.agents import AgentHandle, AgentManager


class SyncCloseAgent:
    def __init__(self):
        self.closed = 0

    def diagnose(self, context):  # pragma: no cover — not exercised here
        raise NotImplementedError

    def close(self):
        self.closed += 1


class AsyncCloseAgent:
    def __init__(self):
        self.closed = 0

    def diagnose(self, context):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self):
        self.closed += 1


class NoCloseAgent:
    def diagnose(self, context):  # pragma: no cover
        raise NotImplementedError


def test_handle_close_invokes_sync_close():
    agent = SyncCloseAgent()
    handle = AgentHandle(agent=agent)
    handle.close()
    assert agent.closed == 1


def test_handle_close_invokes_async_aclose():
    agent = AsyncCloseAgent()
    handle = AgentHandle(agent=agent)
    handle.close()
    assert agent.closed == 1


def test_handle_close_is_safe_when_no_close_method():
    handle = AgentHandle(agent=NoCloseAgent())
    handle.close()  # Must not raise.


def test_handle_close_is_idempotent():
    agent = SyncCloseAgent()
    handle = AgentHandle(agent=agent)
    handle.close()
    handle.close()
    # Underlying agent close called twice but no exception leaks.
    assert agent.closed == 2


def test_handle_close_swallows_exceptions(caplog):
    class Boom:
        def diagnose(self, context):  # pragma: no cover
            raise NotImplementedError

        def close(self):
            raise RuntimeError("boom")

    handle = AgentHandle(agent=Boom(), name="boom")
    handle.close()  # Must not propagate.


def test_manager_close_closes_all_handles():
    a, b = SyncCloseAgent(), AsyncCloseAgent()
    manager = AgentManager()
    manager.wrap(a)
    manager.wrap(b)
    manager.close()
    assert a.closed == 1
    assert b.closed == 1
    # Idempotent and clears tracked handles.
    manager.close()
    assert a.closed == 1


def test_handle_sync_close_in_async_loop_logs_but_does_not_raise(caplog):
    agent = SyncCloseAgent()
    handle = AgentHandle(agent=agent)

    async def runner():
        # AgentHandle.close() catches the RuntimeError raised by _run_async
        # so this must not propagate even though we're inside a loop.
        handle.close()
        # Drain the unawaited aclose() coroutine to avoid a RuntimeWarning
        # leaking into pytest output (best-effort cleanup path).
        await handle.aclose()

    asyncio.run(runner())
    # Sync path is rejected inside an event loop; async aclose() above does the close.
    assert agent.closed == 1


def test_handle_aclose_works_in_async_loop():
    agent = AsyncCloseAgent()
    handle = AgentHandle(agent=agent)

    async def runner():
        await handle.aclose()

    asyncio.run(runner())
    assert agent.closed == 1
