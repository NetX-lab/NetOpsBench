"""Tests for the public NetOpsBench SDK root scaffold."""

import asyncio
import importlib

import pytest


def test_netopsbench_exposes_all_managers():
    from netopsbench.sdk import NetOpsBench

    bench = NetOpsBench()

    for manager_name in (
        "scenarios",
        "agents",
        "faults",
        "runtimes",
        "sessions",
        "artifacts",
        "scales",
        "simulators",
        "evaluators",
    ):
        manager = getattr(bench, manager_name)
        assert manager.platform is bench
        assert manager.name == manager_name


def test_netopsbench_public_manager_api_lives_under_sdk_modules():
    from netopsbench.sdk import AgentManager, EvaluatorManager, RuntimeManager, SessionManager
    from netopsbench.sdk.agents import AgentManager as AgentsModuleAgentManager
    from netopsbench.sdk.evaluators import EvaluatorManager as EvaluatorsModuleEvaluatorManager
    from netopsbench.sdk.runtimes import RuntimeManager as RuntimesModuleRuntimeManager
    from netopsbench.sdk.sessions import SessionManager as SessionsModuleSessionManager

    assert AgentManager is AgentsModuleAgentManager
    assert RuntimeManager is RuntimesModuleRuntimeManager
    assert SessionManager is SessionsModuleSessionManager
    assert EvaluatorManager is EvaluatorsModuleEvaluatorManager
    assert AgentManager.__module__ == "netopsbench.sdk.agents"
    assert RuntimeManager.__module__ == "netopsbench.sdk.runtimes"
    assert SessionManager.__module__ == "netopsbench.sdk.sessions"
    assert EvaluatorManager.__module__ == "netopsbench.sdk.evaluators"


def test_sdk_managers_namespace_is_no_longer_public():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("netopsbench.sdk.managers")


def test_netopsbench_root_only_persists_workspace(tmp_path):
    from netopsbench.sdk import NetOpsBench

    bench = NetOpsBench(workspace=str(tmp_path))

    assert bench.workspace == tmp_path
    assert not hasattr(bench, "defaults")
    assert not hasattr(bench, "env")
    assert not hasattr(bench, "auto_load_env")

    with pytest.raises(TypeError):
        NetOpsBench(env={})


def test_public_api_exports_shared_types():
    from netopsbench.sdk import (
        AgentHandle,
        AgentManager,
        ArtifactManager,
        BuiltinMCPServerHandle,
        DiagnosisResult,
        DiagnosticAgent,
        DiagnosticContext,
        EpisodeSpec,
        EvaluatorManager,
        FaultContext,
        FaultExecutionResult,
        FaultExecutor,
        FaultManager,
        FaultPack,
        FaultRegistry,
        FaultSpec,
        RunHandle,
        RuntimeManager,
        RuntimePool,
        ScenarioManager,
        ScenarioSpec,
        SessionManager,
        SimulatorManager,
        SyncDiagnosticAgent,
        builtin_mcp_server_command,
        builtin_mcp_server_config,
        start_builtin_mcp_server,
    )

    assert ScenarioSpec.__name__ == "ScenarioSpec"
    assert EpisodeSpec.__name__ == "EpisodeSpec"
    assert ScenarioManager.__name__ == "ScenarioManager"
    assert SimulatorManager.__name__ == "SimulatorManager"
    assert EvaluatorManager.__name__ == "EvaluatorManager"
    assert DiagnosticAgent.__name__ == "DiagnosticAgent"
    assert DiagnosticContext.__name__ == "DiagnosticContext"
    assert DiagnosisResult.__name__ == "DiagnosisResult"
    assert SyncDiagnosticAgent.__name__ == "SyncDiagnosticAgent"
    assert AgentHandle.__name__ == "AgentHandle"
    assert AgentManager.__name__ == "AgentManager"
    assert BuiltinMCPServerHandle.__name__ == "BuiltinMCPServerHandle"
    assert callable(builtin_mcp_server_config)
    assert callable(builtin_mcp_server_command)
    assert callable(start_builtin_mcp_server)
    assert FaultContext.__name__ == "FaultContext"
    assert FaultExecutionResult.__name__ == "FaultExecutionResult"
    assert FaultSpec.__name__ == "FaultSpec"
    assert FaultExecutor.__name__ == "FaultExecutor"
    assert FaultPack.__name__ == "FaultPack"
    assert FaultRegistry.__name__ == "FaultRegistry"
    assert FaultManager.__name__ == "FaultManager"
    assert RuntimeManager.__name__ == "RuntimeManager"
    assert RuntimePool.__name__ == "RuntimePool"
    assert SessionManager.__name__ == "SessionManager"
    assert RunHandle.__name__ == "RunHandle"
    assert ArtifactManager.__name__ == "ArtifactManager"


def test_removed_0_1_compatibility_types_are_not_exported():
    import netopsbench.sdk as sdk

    for name in ("ScenarioHandle", "PlatformDefaults", "ScenarioEvaluator"):
        assert not hasattr(sdk, name)


def test_session_orchestrator_is_available_under_platform_session_package():
    from netopsbench.platform.session.orchestrator import SessionOrchestrator

    assert SessionOrchestrator.__module__ == "netopsbench.platform.session.orchestrator"


def test_netopsbench_async_context_closes_wrapped_agents(tmp_path):
    from netopsbench.sdk import NetOpsBench

    class Agent:
        def __init__(self):
            self.closed = 0

        async def aclose(self):
            self.closed += 1

    agent = Agent()

    async def use_bench():
        async with NetOpsBench(workspace=str(tmp_path)) as bench:
            bench.agents.wrap(agent)
            assert bench._closed is False
        assert bench._closed is True

    asyncio.run(use_bench())
    assert agent.closed == 1


def test_netopsbench_sync_close_rejects_running_event_loop(tmp_path):
    from netopsbench.sdk import NetOpsBench

    bench = NetOpsBench(workspace=str(tmp_path))

    async def close_in_loop():
        with pytest.raises(RuntimeError, match=r"await bench\.aclose"):
            bench.close()
        assert bench._closed is False
        await bench.aclose()

    asyncio.run(close_in_loop())
    assert bench._closed is True


def test_netopsbench_aclose_is_retryable_after_agent_failure(tmp_path):
    from netopsbench.sdk import NetOpsBench

    class Flaky:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("cleanup failed")

    bench = NetOpsBench(workspace=str(tmp_path))
    agent = Flaky()
    bench.agents.wrap(agent)

    async def close_twice():
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await bench.aclose()
        assert bench._closed is False
        assert len(bench.agents._handles) == 1
        await bench.aclose()

    asyncio.run(close_twice())
    assert bench._closed is True
    assert agent.calls == 2
