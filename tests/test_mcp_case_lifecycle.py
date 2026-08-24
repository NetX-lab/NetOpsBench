"""Focused contracts for case-scoped MCP lifecycle and recovery."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from examples.agents.diagnostic_harness.models import BaseAgentStatus
from examples.agents.diagnostic_harness.verification.base_reliability import BaseAgentReliability
from examples.agents.minimal_deepagent.providers.runtime import (
    MCPCaseClient,
    MCPInfrastructureError,
    MCPLifecycleTrace,
)
from netopsbench.sdk.agents import DiagnosisResult


class FakeSession:
    def __init__(self, *, initialize_error=None, list_error=None, calls=()):
        self.initialize_error = initialize_error
        self.list_error = list_error
        self.calls = deque(calls)
        self.call_count = 0

    async def initialize(self):
        if self.initialize_error:
            raise self.initialize_error

    async def list_tools(self):
        if self.list_error:
            raise self.list_error
        return SimpleNamespace(tools=[SimpleNamespace(name="get_topology")])

    async def call_tool(self, name, arguments):
        self.call_count += 1
        value = self.calls.popleft() if self.calls else {"ok": True, "name": name, "arguments": arguments}
        if isinstance(value, BaseException):
            raise value
        return value


class FakeSessionContext:
    def __init__(self, factory, session):
        self.factory = factory
        self.session = session

    async def __aenter__(self):
        self.factory.active += 1
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        self.factory.active -= 1
        self.factory.closed += 1
        return False


class FakeSessionFactory:
    def __init__(self, sessions):
        self.sessions = deque(sessions)
        self.created = 0
        self.closed = 0
        self.active = 0

    def __call__(self, connection):
        del connection
        self.created += 1
        return FakeSessionContext(self, self.sessions.popleft())


class FakeBoundTool:
    def __init__(self, name, session, interceptor):
        self.name = name
        self.session = session
        self.interceptor = interceptor

    async def ainvoke(self, arguments):
        request = SimpleNamespace(name=self.name, args=arguments)

        async def handler(current_request):
            return await self.session.call_tool(current_request.name, current_request.args)

        return await self.interceptor(request, handler)


async def fake_tool_loader(session, *, tool_interceptors, **kwargs):
    del kwargs
    return [FakeBoundTool("get_topology", session, tool_interceptors[0])]


async def fake_active_tool_loader(session, *, tool_interceptors, **kwargs):
    del kwargs
    return [FakeBoundTool("ping_test", session, tool_interceptors[0])]


def run(coro):
    return asyncio.run(coro)


async def _one_case(case_id, factory, *, loader=fake_tool_loader, timeout=0.1):
    trace = MCPLifecycleTrace(case_id)
    async with MCPCaseClient(
        {"netopsbench": {"transport": "stdio"}},
        trace,
        health_timeout_seconds=timeout,
        session_factory=factory,
        tool_loader=loader,
    ) as client:
        assert await client.tools[0].ainvoke({}) == {"ok": True, "name": client.tools[0].name, "arguments": {}}
    return trace


def test_case_client_health_check_and_cleanup_are_balanced():
    factory = FakeSessionFactory([FakeSession()])
    trace = run(_one_case("case-1", factory))

    assert trace.metrics.mcp_client_created == 1
    assert trace.metrics.mcp_client_closed == 1
    assert trace.metrics.mcp_session_created == 1
    assert trace.metrics.mcp_session_closed == 1
    assert trace.metrics.mcp_live_clients_after_case == 0
    assert factory.active == 0


def test_closed_client_between_cases_is_not_reused():
    factory = FakeSessionFactory([FakeSession(), FakeSession()])
    first = run(_one_case("case-1", factory))
    second = run(_one_case("case-2", factory))

    first_id = next(event["session_id"] for event in first.events if event["event"] == "session_created")
    second_id = next(event["session_id"] for event in second.events if event["event"] == "session_created")
    assert first_id != second_id
    assert factory.created == factory.closed == 2


def test_pre_case_health_failure_recreates_once():
    factory = FakeSessionFactory(
        [
            FakeSession(initialize_error=RuntimeError("Connection closed")),
            FakeSession(),
        ]
    )
    trace = run(_one_case("case-recreate", factory))

    assert trace.infrastructure_degraded is True
    assert trace.metrics.mcp_reconnect_attempts == 1
    assert trace.metrics.mcp_reconnect_successes == 1
    assert trace.metrics.mcp_reconnect_failures == 0
    assert factory.created == factory.closed == 2


def test_midcase_disconnect_retries_query_on_new_session():
    first = FakeSession(calls=[RuntimeError("Connection closed")])
    second = FakeSession(calls=[{"recovered": True}])
    factory = FakeSessionFactory([first, second])
    trace = MCPLifecycleTrace("case-midcall")

    async def scenario():
        async with MCPCaseClient(
            {"netopsbench": {"transport": "stdio"}},
            trace,
            session_factory=factory,
            tool_loader=fake_tool_loader,
        ) as client:
            tool = client.tools[0]
            assert await tool.ainvoke({"detail": "small"}) == {"recovered": True}
            # The original tool binding must dispatch to the new generation.
            assert (await tool.ainvoke({"detail": "again"}))["ok"] is True

    run(scenario())
    assert first.call_count == 1
    assert second.call_count == 2
    assert trace.metrics.mcp_midcase_disconnects == 1
    assert trace.metrics.mcp_reconnect_successes == 1
    retries = [event for event in trace.events if event["event"] == "tool_retry"]
    assert len(retries) == 1
    assert retries[0]["tool_name"] == "get_topology"


def test_midcase_active_probe_is_not_replayed():
    first = FakeSession(calls=[RuntimeError("transport closed")])
    second = FakeSession()
    factory = FakeSessionFactory([first, second])
    trace = MCPLifecycleTrace("case-active")

    async def scenario():
        async with MCPCaseClient(
            {"netopsbench": {"transport": "stdio"}},
            trace,
            session_factory=factory,
            tool_loader=fake_active_tool_loader,
        ) as client:
            with pytest.raises(MCPInfrastructureError, match="not replayed"):
                await client.tools[0].ainvoke({"count": 20})

    run(scenario())
    assert first.call_count == 1
    assert second.call_count == 0
    assert not [event for event in trace.events if event["event"] == "tool_retry"]


def test_reconnect_failure_is_infrastructure_not_fault_evidence():
    first = FakeSession(calls=[RuntimeError("Connection closed")])
    second = FakeSession(initialize_error=RuntimeError("Connection closed"))
    factory = FakeSessionFactory([first, second])
    trace = MCPLifecycleTrace("case-failed-reconnect")

    async def scenario():
        with pytest.raises(MCPInfrastructureError, match="recreate failed"):
            async with MCPCaseClient(
                {"netopsbench": {"transport": "stdio"}},
                trace,
                session_factory=factory,
                tool_loader=fake_tool_loader,
            ) as client:
                await client.tools[0].ainvoke({})

    run(scenario())
    result = DiagnosisResult(
        agent_name="base",
        verdict="inconclusive",
        success=False,
        findings={"fault_type": None, "evidence": [], "error": "MCP client unavailable"},
        metadata={"mcp_lifecycle": trace.as_dict(), "error_type": "MCPInfrastructureError"},
    )
    assessment = BaseAgentReliability().assess(result)
    assert assessment.status is BaseAgentStatus.TOOL_INFRASTRUCTURE_FAILED
    assert result.findings["evidence"] == []
    assert trace.metrics.mcp_reconnect_failures == 1


def test_successful_external_evidence_survives_reconnect():
    evidence = ["E-before-disconnect"]
    first = FakeSession(calls=[RuntimeError("stream closed")])
    second = FakeSession(calls=[{"ok": True}])
    factory = FakeSessionFactory([first, second])
    trace = MCPLifecycleTrace("case-evidence")

    async def scenario():
        async with MCPCaseClient(
            {"netopsbench": {"transport": "stdio"}},
            trace,
            session_factory=factory,
            tool_loader=fake_tool_loader,
        ) as client:
            await client.tools[0].ainvoke({})

    run(scenario())
    assert evidence == ["E-before-disconnect"]


def test_only_one_recreate_is_allowed_per_case():
    first = FakeSession(calls=[RuntimeError("Connection closed")])
    second = FakeSession(calls=[RuntimeError("Connection closed")])
    factory = FakeSessionFactory([first, second])
    trace = MCPLifecycleTrace("case-bounded")

    async def scenario():
        async with MCPCaseClient(
            {"netopsbench": {"transport": "stdio"}},
            trace,
            session_factory=factory,
            tool_loader=fake_tool_loader,
        ) as client:
            with pytest.raises(MCPInfrastructureError, match="bounded reconnect"):
                tool = client.tools[0]
                await tool.ainvoke({})

    run(scenario())
    assert factory.created == 2
    assert trace.metrics.mcp_reconnect_attempts == 1


def test_timeout_does_not_poison_next_case():
    class SlowSession(FakeSession):
        async def initialize(self):
            await asyncio.sleep(1)

    first_factory = FakeSessionFactory([SlowSession(), SlowSession()])
    first_trace = MCPLifecycleTrace("case-timeout")

    async def timeout_case():
        with pytest.raises(MCPInfrastructureError):
            async with MCPCaseClient(
                {"netopsbench": {"transport": "stdio"}},
                first_trace,
                health_timeout_seconds=0.001,
                session_factory=first_factory,
                tool_loader=fake_tool_loader,
            ):
                pass

    run(timeout_case())
    next_factory = FakeSessionFactory([FakeSession()])
    next_trace = run(_one_case("case-after-timeout", next_factory))
    assert next_trace.infrastructure_degraded is False
    assert next_trace.metrics.mcp_live_clients_after_case == 0


def test_thirty_case_endurance_has_no_live_client_growth():
    sessions = [FakeSession() for _ in range(30)]
    factory = FakeSessionFactory(sessions)
    traces = [run(_one_case(f"case-{index}", factory)) for index in range(30)]

    assert factory.created == factory.closed == 30
    assert factory.active == 0
    assert all(trace.metrics.mcp_live_clients_after_case == 0 for trace in traces)
    assert max(trace.metrics.mcp_pending_tasks_after_case for trace in traces) == 0


def test_lifecycle_trace_does_not_persist_exception_message_or_secret():
    secret = "do-not-log-this-key"
    factory = FakeSessionFactory(
        [
            FakeSession(initialize_error=RuntimeError(f"Connection closed Authorization={secret}")),
            FakeSession(),
        ]
    )
    trace = run(_one_case("case-secret", factory))

    serialized = str(trace.as_dict())
    assert secret not in serialized
    assert "Authorization=" not in serialized
