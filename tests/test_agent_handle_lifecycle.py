"""Tests for :class:`AgentHandle` / :class:`AgentManager` lifecycle hooks."""

from __future__ import annotations

import asyncio

import pytest

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
    assert agent.closed == 1


def test_handle_close_propagates_exceptions_for_retry():
    class Boom:
        def diagnose(self, context):  # pragma: no cover
            raise NotImplementedError

        def close(self):
            raise RuntimeError("boom")

    handle = AgentHandle(agent=Boom(), name="boom")
    with pytest.raises(RuntimeError, match="boom"):
        handle.close()


def test_handle_close_can_retry_after_underlying_failure():
    class Flaky:
        def __init__(self):
            self.calls = 0

        def diagnose(self, context):  # pragma: no cover
            raise NotImplementedError

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")

    agent = Flaky()
    handle = AgentHandle(agent=agent, name="flaky")
    with pytest.raises(RuntimeError, match="boom"):
        handle.close()
    handle.close()

    assert agent.calls == 2


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


def test_handle_sync_close_in_async_loop_has_explicit_error():
    agent = SyncCloseAgent()
    handle = AgentHandle(agent=agent)

    async def runner():
        with pytest.raises(RuntimeError, match=r"await agent\.aclose"):
            handle.close()
        await handle.aclose()

    asyncio.run(runner())
    # Sync path is rejected inside an event loop; async aclose() above does the close.
    assert agent.closed == 1


def test_manager_aclose_retains_only_failed_handles_for_retry():
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("retry me")

    stable = AsyncCloseAgent()
    flaky = Flaky()
    manager = AgentManager()
    manager.wrap(stable)
    manager.wrap(flaky)

    async def runner():
        with pytest.raises(RuntimeError, match="retry me"):
            await manager.aclose()
        assert len(manager._handles) == 1
        await manager.aclose()

    asyncio.run(runner())
    assert stable.closed == 1
    assert flaky.calls == 2


def test_handle_aclose_works_in_async_loop():
    agent = AsyncCloseAgent()
    handle = AgentHandle(agent=agent)

    async def runner():
        await handle.aclose()

    asyncio.run(runner())
    assert agent.closed == 1
