import asyncio
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from examples.agents.codex_agent.agent import CodexAgent, CodexEvents, redact, strict_output_schema
from examples.agents.codex_agent.proxy import validate_destination
from examples.agents.codex_agent.runtime import DiagnosticContainer, ensure_image, image_content_identity
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp.session_server import session_mcp
from netopsbench.sdk.agents import DiagnosticContext


@pytest.fixture
def isolated_agent(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager, nullcontext

    import examples.agents.codex_agent.agent as module

    @asynccontextmanager
    async def endpoint(*a, **kw):
        yield {"url": "http://127.0.0.1:1/mcp", "token": "private-token", "port": 1}

    store = tmp_path / "auth"
    store.mkdir()
    (store / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"test-secret"}}')
    monkeypatch.setattr(module, "session_mcp", endpoint)
    monkeypatch.setattr(module, "openai_proxy", lambda *a: nullcontext(1))

    def create(container, **kwargs):
        monkeypatch.setattr(module, "DiagnosticContainer", container)
        agent = CodexAgent(auth_store=store, artifacts_dir=tmp_path / "traces", **kwargs)
        agent._image_id = "pinned-image"
        return agent

    return create


def result_events():
    yield {
        "type": "item.completed",
        "item": {
            "id": "final",
            "type": "agent_message",
            "text": json.dumps(
                {
                    "verdict": "inconclusive",
                    "fault_type": None,
                    "location": {"device": None, "interface": None},
                    "evidence": [],
                    "confidence": 0.2,
                    "reasoning": "insufficient evidence",
                }
            ),
        },
    }
    yield {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3, "cached_input_tokens": 5}}


def test_native_events_validate_final_and_deduplicate_tool_items():
    events = CodexEvents()
    for kind in ("item.started", "item.completed"):
        events.accept({"type": kind, "item": {"id": "tool", "type": "mcp_tool_call"}})
    for event in result_events():
        events.accept(event)
    assert events.result(0)["verdict"] == "inconclusive"
    assert events.counts()["mcp_calls"] == 1
    assert events.usage["total_tokens"] == 14
    assert "llm_call_count" not in events.usage
    with pytest.raises(RuntimeError):
        events.result(1)
    events.accept({"type": "turn.failed", "error": {"message": "failed"}})
    with pytest.raises(RuntimeError, match="failed"):
        events.result(0)


def test_missing_and_invalid_native_output_fail():
    events = CodexEvents()
    events.accept({"type": "turn.completed"})
    with pytest.raises(ValueError, match="structured"):
        events.result(0)
    events.final = '{"verdict":"invented"}'
    with pytest.raises(ValueError):
        events.result(0)


def test_output_schema_and_cli_do_not_inherit_user_agent_state():
    schema = strict_output_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    args = CodexAgent().cli_args({"url": "http://192.0.2.1:1000/mcp"})
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema"):
        assert flag in args
    assert 'web_search="disabled"' in args
    assert "features.multi_agent=false" in args
    assert "features.apps=false" in args
    assert "mcp_servers.netopsbench.required=true" in args
    assert 'service_tier="default"' in args
    assert "features.fast_mode=false" in args
    assert "features.ultrafast_mode=false" in args
    assert CodexAgent().service_tier == "default"


def test_image_content_identity_survives_store_conversion_but_detects_execution_changes(monkeypatch):
    image = {
        "Id": "manifest-id",
        "Architecture": "amd64",
        "Os": "linux",
        "RootFS": {"Type": "layers", "Layers": ["sha256:layer"]},
        "Config": {"Entrypoint": ["/entrypoint.sh"], "Env": ["PATH=/bin"]},
    }
    monkeypatch.setattr("examples.agents.codex_agent.runtime.docker", lambda *args: json.dumps([image]))
    expected = image_content_identity("image")
    image["Id"] = "config-id"
    image["Config"].update(User="", WorkingDir="", StopSignal="", AttachStdin=False)
    assert image_content_identity("image") == expected
    image["Config"]["Entrypoint"] = ["/other.sh"]
    assert image_content_identity("image") != expected
    image["Config"]["Entrypoint"] = ["/entrypoint.sh"]
    image["RootFS"]["Layers"] = ["sha256:other-layer"]
    assert image_content_identity("image") != expected


def test_secret_redaction_and_proxy_private_destination_rejection(monkeypatch):
    assert redact("secret-token Bearer abc.def", ["secret-token"]) == "[REDACTED] Bearer [REDACTED]"
    with pytest.raises(ValueError):
        validate_destination("example.com:443")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ValueError, match="Non-public"):
        validate_destination("chatgpt.com:443")


def test_private_credential_values_are_redacted_including_account_identity(tmp_path):
    from examples.agents.codex_agent.runtime import credentials

    payload = {"tokens": {"access_token": "private-token", "account_id": "private-account"}}
    (tmp_path / "auth.json").write_text(json.dumps(payload))
    values = credentials(tmp_path)
    cleaned = redact(payload, values)
    assert "private-token" not in cleaned
    assert "private-account" not in cleaned
    assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_episode_mcp_requires_token_uses_gateway_and_enforces_budget():
    import signal

    import httpx
    from fastmcp import Client

    original_handler = signal.getsignal(signal.SIGTERM)
    calls = []
    context = DiagnosticContext(
        "case-anonymous",
        {},
        {},
        tools=SimpleNamespace(get_topology=lambda **kw: calls.append(kw) or ToolResult(True, {"window": "episode"})),
    )
    async with session_mcp(context, max_tool_calls=1) as endpoint:
        assert signal.getsignal(signal.SIGTERM) == original_handler
        async with Client(endpoint["url"], auth=endpoint["token"]) as client:
            result = await client.call_tool("get_topology", {})
            assert result.data["data"] == {"window": "episode"}
            with pytest.raises(Exception, match="budget"):
                await client.call_tool("get_topology", {})
        # Verify the HTTP boundary directly: MCP clients can wrap an HTTP 401
        # in a generic transport error whose wording varies by dependency version.
        async with httpx.AsyncClient(trust_env=False) as client:
            for headers in ({}, {"Authorization": "Bearer incorrect-token"}):
                response = await client.post(endpoint["url"], headers=headers, json={})
                assert response.status_code == 401
        assert calls == [{}]
    assert signal.getsignal(signal.SIGTERM) == original_handler


@pytest.mark.asyncio
async def test_episode_http_tools_inherit_the_agent_deadline():
    import time

    from fastmcp import Client

    from netopsbench.platform.toolkit._core.control import current_tool_control, tool_deadline_scope

    deadline = time.monotonic() + 20
    context = DiagnosticContext(
        "case-anonymous",
        {},
        {},
        tools=SimpleNamespace(
            get_topology=lambda **kw: ToolResult(True, {"deadline": current_tool_control().deadline})
        ),
    )
    with tool_deadline_scope(deadline):
        async with session_mcp(context) as endpoint:
            async with Client(endpoint["url"], auth=endpoint["token"]) as client:
                result = await client.call_tool("get_topology", {})
                assert result.data["data"]["deadline"] == deadline


@pytest.mark.real
@pytest.mark.asyncio
async def test_real_diagnostic_container_blocks_host_files_privileges_and_network(tmp_path):
    ensure_image()
    auth = tmp_path / "auth"
    auth.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    inputs.chmod(0o755)
    probe = inputs / "probe.py"
    probe.write_text("""import json, os, socket, urllib.request
from pathlib import Path
assert not Path('/var/run/docker.sock').exists()
assert not Path('/local/ytyang/NetOpsBench-NetX').exists()
assert os.getuid() != 0
status = Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status
assert 'CapBnd:\\t0000000000000000' in status
for destination in [(os.environ['DIAGNOSTIC_GATEWAY'], 22), ('172.31.224.2', 22), ('1.1.1.1', 443)]:
 try:
  socket.create_connection(destination, timeout=1).close()
 except OSError:
  continue
 raise AssertionError('Forbidden destination was reachable')
url = 'http://' + os.environ['DIAGNOSTIC_GATEWAY'] + ':' + os.environ['DIAGNOSTIC_MCP_PORT'] + '/mcp'
body = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_topology','arguments':{}}}).encode()
request = urllib.request.Request(url, data=body, headers={'Authorization':'Bearer '+os.environ['DIAGNOSTIC_MCP_TOKEN'],
 'Content-Type':'application/json', 'Accept':'application/json,text/event-stream'})
response = urllib.request.urlopen(request, timeout=10)
assert response.status == 200 and 'isolation-ok' in response.read().decode()
print('isolation and authenticated MCP passed')
""")
    probe.chmod(0o644)
    context = DiagnosticContext(
        "isolation",
        {},
        {},
        tools=SimpleNamespace(get_topology=lambda **kw: ToolResult(True, {"probe": "isolation-ok"})),
    )
    container = DiagnosticContainer("netopsbench-codex:0.159.2", auth)
    await asyncio.to_thread(container.open)
    try:
        async with session_mcp(context, host=container.gateway) as endpoint:
            command = container.command(
                inputs,
                mcp_port=endpoint["port"],
                proxy_port=9,
                token=endpoint["token"],
                cli_args=["python3", "/input/probe.py"],
            )
            process = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, errors = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, errors.decode()
            assert b"authenticated MCP passed" in output
    finally:
        await asyncio.to_thread(container.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["nonzero", "invalid", "timeout", "startup_timeout", "cancel"])
async def test_native_process_failures_always_release_resources(monkeypatch, tmp_path, isolated_agent, failure):
    import sys
    import time

    import examples.agents.codex_agent.agent as module

    releases = []
    script = (
        "import sys,time; sys.stdin.read(); time.sleep(100)"
        if failure in {"timeout", "cancel"}
        else (
            'import sys; sys.stdin.read(); print("{}",flush=True); sys.exit(1)'
            if failure == "nonzero"
            else 'import sys; sys.stdin.read(); print(\'{"type":"turn.completed"}\',flush=True)'
        )
    )

    class Container:
        def __init__(self, image, auth_store):
            self.auth_store = auth_store
            self.gateway = "127.0.0.1"

        def open(self):
            if failure == "startup_timeout":
                time.sleep(0.08)
            return self

        def command(self, *a, **kw):
            return [sys.executable, "-c", script]

        def close(self):
            releases.append(True)

    if failure == "startup_timeout":
        monkeypatch.setattr(
            module.asyncio,
            "create_subprocess_exec",
            lambda *a, **kw: pytest.fail("Expired startup must not launch CLI"),
        )
    agent = isolated_agent(Container, timeout_seconds=0.05)
    context = DiagnosticContext("anonymous", {}, {}, tools=None)
    task = asyncio.create_task(agent.diagnose(context))
    if failure == "cancel":
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.success is False
        assert result.metadata["agent_failure_stage"] == "diagnose"
        if failure == "startup_timeout":
            assert result.metadata["error_type"] == "TimeoutError"
    assert releases == [True]
    assert not agent._containers
    store = tmp_path / "auth"
    assert list(store.iterdir()) == [store / "auth.json"]


@pytest.mark.asyncio
async def test_cancellation_joins_slow_startup_before_cleanup(isolated_agent):
    import threading

    started, release = threading.Event(), threading.Event()
    steps = []

    class Container:
        def __init__(self, image, auth_store):
            self.auth_store = auth_store

        def open(self):
            started.set()
            release.wait(3)
            steps.append("opened")

        def close(self):
            steps.append("closed")

    agent = isolated_agent(Container)
    task = asyncio.create_task(agent.diagnose(DiagnosticContext("anonymous", {}, {}, tools=None)))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0.02)
    assert steps == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert steps == ["opened", "closed"]
    assert not agent._containers


@pytest.mark.parametrize("failure", ["timeout", "delete", "already_missing"])
def test_container_cleanup_checks_absence_and_attempts_network_after_failure(monkeypatch, tmp_path, failure):
    import examples.agents.codex_agent.runtime as module

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["rm", "-f"] and failure == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if command[1] == "inspect":
            if failure == "delete" and "container" in command:
                return subprocess.CompletedProcess(command, 0, stdout="exists", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Error: No such object")
        return subprocess.CompletedProcess(
            command, 1 if failure == "already_missing" else 0, stdout="", stderr="permission denied"
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    container = DiagnosticContainer("image", tmp_path)
    if failure == "already_missing":
        container.close()
    else:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            container.close()
    assert ["docker", "network", "rm", container.network] in commands


@pytest.mark.parametrize("host_newer", [True, False])
def test_private_credentials_follow_newer_host_login_without_losing_case_refresh(monkeypatch, tmp_path, host_newer):
    from pathlib import Path

    from examples.agents.codex_agent.runtime import credentials

    host = tmp_path / "host"
    (host / ".codex").mkdir(parents=True)
    store = tmp_path / "private"
    store.mkdir()
    earlier, later = "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z"
    (host / ".codex/auth.json").write_text(
        json.dumps(
            {
                "last_refresh": later if host_newer else earlier,
                "tokens": {"access_token": "host-token"},
            }
        )
    )
    (store / "auth.json").write_text(
        json.dumps(
            {
                "last_refresh": earlier if host_newer else later,
                "tokens": {"access_token": "case-token"},
            }
        )
    )
    monkeypatch.setattr(Path, "home", lambda: host)
    secrets = credentials(store, refresh_from_host=True)
    assert secrets == ["host-token" if host_newer else "case-token"]
    assert (store / "auth.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported_without_mutating_frozen_result(isolated_agent):
    import sys

    class Container:
        name, network, gateway = "owned-container", "owned-network", "127.0.0.1"

        def __init__(self, image, auth_store):
            self.auth_store = auth_store

        def open(self):
            pass

        def command(self, *a, **kw):
            events = list(result_events())
            script = "import sys;sys.stdin.read();" + ";".join(f"print({json.dumps(event)!r})" for event in events)
            return [sys.executable, "-c", script]

        def close(self):
            raise RuntimeError("Docker removal failed")

    agent = isolated_agent(Container)
    result = await agent.diagnose(DiagnosticContext("anonymous", {}, {}, tools=None))
    assert result.success is False
    assert result.metadata["cleanup_errors"] == ["Docker removal failed"]
    assert result.metadata["retained_resources"]["container"] == "owned-container"
    assert len(agent._containers) == 1
    assert all(not container.auth_store.exists() for container in agent._containers)
