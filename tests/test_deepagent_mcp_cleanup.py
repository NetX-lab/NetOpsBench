"""Exercise actual MCP lifecycles without calling a model or starting a lab."""

import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = r"""
import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

import examples.agents.minimal_deepagent.agent as module
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.sdk.agents import DiagnosticContext

mode = sys.argv[1]
ports, calls = [], []
original_endpoint = module.session_mcp

@asynccontextmanager
async def endpoint(*args, **kwargs):
    async with original_endpoint(*args, **kwargs) as connection:
        ports.append(connection["port"])
        yield connection

async def exercise():
    started = asyncio.Event()

    def create_graph(**kwargs):
        tool = next(tool for tool in kwargs["tools"] if tool.name == "get_topology")

        async def invoke(*args, **kwargs):
            assert "healthy" in str(await tool.ainvoke({}))
            started.set()
            if mode in {"cancel", "timeout"}:
                await asyncio.sleep(600)
            if mode == "failure":
                raise ValueError("model failed")
            payload = {
                "verdict": "network_healthy", "fault_type": None,
                "location": {"device": None, "interface": None},
                "evidence": ["Healthy observation"], "confidence": 0.9,
                "reasoning": "Healthy.",
            }
            return {"messages": [AIMessage(content="```json\n" + json.dumps(payload) + "\n```")]}

        return SimpleNamespace(ainvoke=invoke)

    context = DiagnosticContext(
        "anonymous", {}, {},
        metadata={"canonical_observation": {"case_id": "anonymous"}},
        tools=SimpleNamespace(get_topology=lambda **kw: calls.append(kw) or ToolResult(True, {"healthy": True})),
    )
    provider = module.get_provider("deepseek")
    with (
        patch.object(module, "session_mcp", endpoint),
        patch.object(module, "create_deep_agent", create_graph),
        patch.object(provider, "build_llm", lambda **kwargs: object()),
    ):
        agent = module.MinimalDeepAgent(
            vendor="deepseek", api_key="offline-test",
            diagnosis_timeout_seconds=5 if mode == "timeout" else 20,
        )
        task = asyncio.create_task(agent.diagnose(context))
        await asyncio.wait_for(started.wait(), 15)
        if mode == "cancel":
            task.cancel()
            try:
                await asyncio.wait_for(task, 15)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation must propagate")
        else:
            result = await asyncio.wait_for(task, 15)
            if mode == "normal":
                assert result.success is True
                assert result.verdict == "network_healthy"
            else:
                assert result.success is False
                expected = "timeout" if mode == "timeout" else "model failed"
                assert expected in result.reasoning.lower()
    assert calls == [{}]

asyncio.run(exercise())
assert ports
for port in ports:
    with socket.socket() as listener:
        listener.settimeout(0.5)
        assert listener.connect_ex(("127.0.0.1", port)) != 0, "MCP listener leaked"
"""


@pytest.mark.parametrize("mode", ["normal", "failure", "cancel", "timeout"])
def test_deepagent_closes_real_mcp_session_on_owning_task(mode):
    for dependency in ("deepagents", "fastmcp", "langchain-mcp-adapters"):
        try:
            importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            pytest.skip(f"Optional agent dependency unavailable: {dependency}")
    # Other example tests stub optional modules in sys.modules; use a fresh
    # interpreter so this regression always exercises the real MCP transport.
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, mode],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
