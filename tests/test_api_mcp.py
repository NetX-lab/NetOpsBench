"""Tests for the public SDK MCP helpers."""

from __future__ import annotations

from pathlib import Path

from netopsbench.sdk.mcp import builtin_mcp_server_command, builtin_mcp_server_config, start_builtin_mcp_server


def test_builtin_mcp_server_config_has_expected_shape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    config = builtin_mcp_server_config(workspace=repo)

    assert "netopsbench" in config
    server = config["netopsbench"]
    assert server["transport"] == "stdio"
    assert server["args"] == ["scripts/toolkit/run_fastmcp_server.py"]
    assert Path(server["cwd"]) == repo.resolve()
    assert server["env"]["PYTHONPATH"] == str(repo.resolve())


def test_builtin_mcp_server_command_uses_python_and_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    command = builtin_mcp_server_command(workspace=repo)

    assert command["args"] == ["scripts/toolkit/run_fastmcp_server.py"]
    assert command["cwd"] == str(repo.resolve())
    assert command["command"]


def test_start_builtin_mcp_server_returns_stoppable_handle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    calls = {}

    class _FakePopen:
        def __init__(self, args, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
            calls["args"] = args
            calls["cwd"] = cwd
            calls["env"] = env
            self.pid = 12345
            self._returncode = None

        def poll(self):
            return self._returncode

        def terminate(self):
            self._returncode = 0

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    monkeypatch.setattr("netopsbench.sdk.mcp.subprocess.Popen", _FakePopen)

    handle = start_builtin_mcp_server(workspace=repo)

    assert handle.pid == 12345
    assert calls["args"][1:] == ["scripts/toolkit/run_fastmcp_server.py"]
    assert calls["cwd"] == str(repo.resolve())

    handle.stop()
    assert handle.poll() == 0
