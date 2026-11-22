from types import SimpleNamespace

import pytest
from fastmcp import Client

from examples.agents import AntigravityAgent
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.sdk.agents import DiagnosticContext


@pytest.mark.asyncio
@pytest.mark.parametrize("query_mcp", [True, False])
async def test_antigravity_requires_episode_mcp_and_maps_usage(monkeypatch, tmp_path, query_mcp):
    monkeypatch.setattr("examples.agents.antigravity_agent.shutil.which", lambda name: name)
    binary = tmp_path / "agy"
    binary.touch()
    agent = AntigravityAgent(binary=binary)
    context = DiagnosticContext(
        "case", {}, {}, tools=SimpleNamespace(get_topology=lambda: ToolResult(True, {"leaves": []}))
    )

    async def run(context, endpoint, timeout):
        if query_mcp:
            async with Client(endpoint["url"], auth=endpoint["token"]) as client:
                await client.call_tool("get_topology", {})
        return {
            "status": "SUCCESS",
            "structured_output": {"verdict": "network_healthy"},
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        }

    monkeypatch.setattr(agent, "_run", run)
    result = await agent.diagnose(context)
    assert result.metadata["total_tokens"] == 15
    assert result.success is query_mcp
    if query_mcp:
        assert result.verdict == "network_healthy"
        assert [call["tool"] for call in result.metadata["tool_calls"]] == ["get_topology"]
    else:
        assert "without querying episode MCP" in result.reasoning


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", [b"null", b"[]"])
async def test_antigravity_handles_non_object_cli_output(monkeypatch, tmp_path, stdout):
    binary = tmp_path / "agy"
    binary.touch()
    monkeypatch.setattr("examples.agents.antigravity_agent.shutil.which", lambda name: name)
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    async def communicate():
        return stdout, b""

    async def start(*args, **kwargs):
        return SimpleNamespace(communicate=communicate, returncode=0)

    monkeypatch.setattr("examples.agents.antigravity_agent.asyncio.create_subprocess_exec", start)
    result = await AntigravityAgent(binary=binary).diagnose(DiagnosticContext("case", {}, {}, tools=SimpleNamespace()))
    assert result.verdict == "inconclusive"
    assert not result.success
    assert "JSON response that is not an object" in result.reasoning
