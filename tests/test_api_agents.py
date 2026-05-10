"""Tests for the public SDK-only agent contract."""

import asyncio

import pytest

from netopsbench.sdk.agents import AgentHandle, AgentManager
from netopsbench.sdk.types import DiagnosisResult, DiagnosticContext


class SyncAgent:
    def diagnose(self, context):
        return DiagnosisResult(
            agent_name="sync_agent",
            verdict="inconclusive",
            success=True,
            findings={"echo": context.scenario_id},
            confidence=0.25,
            reasoning="sync path",
        )


class AsyncAgent:
    async def diagnose(self, context):
        return DiagnosisResult(
            agent_name="async_agent",
            verdict="network_healthy",
            success=True,
            findings={"async": context.scenario_id},
            confidence=0.5,
            reasoning="async path",
        )


class ProcessOnlyAgent:
    async def process(self, context):
        return DiagnosisResult(
            agent_name="legacy_process",
            verdict="inconclusive",
        )


def _make_context() -> DiagnosticContext:
    return DiagnosticContext(
        scenario_id="scenario-1",
        topology={"devices": {}},
        symptoms={"observations": {}},
    )


def test_agent_handle_wraps_sync_agent_into_async_diagnose():
    handle = AgentHandle(SyncAgent(), name="sync")

    result = asyncio.run(handle.diagnose(_make_context()))

    assert result.verdict == "inconclusive"
    assert result.findings == {"echo": "scenario-1"}
    assert handle.name == "sync"


def test_agent_handle_supports_async_diagnose_agents():
    handle = AgentHandle(AsyncAgent(), name="async")

    result = asyncio.run(handle.diagnose(_make_context()))

    assert result.verdict == "network_healthy"
    assert result.findings == {"async": "scenario-1"}
    assert handle.name == "async"


def test_agent_handle_rejects_legacy_process_only_agents():
    handle = AgentHandle(ProcessOnlyAgent(), name="legacy")

    with pytest.raises(AttributeError, match=r"diagnose\(\)"):
        asyncio.run(handle.diagnose(_make_context()))


def test_agent_manager_wraps_explicit_agent_instance_only():
    manager = AgentManager()

    handle = manager.wrap(SyncAgent(), name="wrapped")

    assert isinstance(handle, AgentHandle)
    assert handle.name == "wrapped"
    assert not hasattr(manager, "load")


def test_sdk_agent_module_reexports_public_types_from_sdk_types():
    from netopsbench.sdk import DiagnosisResult as RootDiagnosisResult
    from netopsbench.sdk import DiagnosticContext as RootDiagnosticContext
    from netopsbench.sdk.agents import DiagnosisResult as AgentDiagnosisResult
    from netopsbench.sdk.agents import DiagnosticContext as AgentDiagnosticContext

    assert AgentDiagnosticContext is DiagnosticContext
    assert AgentDiagnosisResult is DiagnosisResult
    assert RootDiagnosticContext is DiagnosticContext
    assert RootDiagnosisResult is DiagnosisResult
