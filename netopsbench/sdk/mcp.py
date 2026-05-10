"""Public SDK helpers for the built-in NetOpsBench MCP server."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class BuiltinMCPServerHandle:
    """Handle for a spawned built-in MCP server process."""

    process: subprocess.Popen

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self, timeout: float = 5.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)

    def __enter__(self) -> BuiltinMCPServerHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def builtin_mcp_server_config(
    workspace: str | Path = ".",
    *,
    env: Mapping[str, str] | None = None,
    python_executable: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return stdio MCP server config used by NetOpsBench built-in tools."""

    repo_root = Path(workspace).resolve()
    passthrough_env = {key: value for key, value in os.environ.items() if key.startswith("NETOPSBENCH_") and value}
    if env:
        passthrough_env.update({k: v for k, v in env.items() if isinstance(v, str) and v})

    return {
        "netopsbench": {
            "transport": "stdio",
            "command": python_executable or sys.executable,
            "args": ["scripts/toolkit/run_fastmcp_server.py"],
            "cwd": str(repo_root),
            "env": {
                **passthrough_env,
                "PYTHONPATH": str(repo_root),
            },
        }
    }


def builtin_mcp_server_command(
    workspace: str | Path = ".",
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Return command details for starting the built-in MCP server."""

    config = builtin_mcp_server_config(workspace=workspace, python_executable=python_executable)
    server = config["netopsbench"]
    return {
        "command": server["command"],
        "args": list(server["args"]),
        "cwd": server["cwd"],
    }


def start_builtin_mcp_server(
    workspace: str | Path = ".",
    *,
    env: Mapping[str, str] | None = None,
    python_executable: str | None = None,
) -> BuiltinMCPServerHandle:
    """Spawn the built-in MCP server process and return a controllable handle."""

    config = builtin_mcp_server_config(workspace=workspace, env=env, python_executable=python_executable)
    server = config["netopsbench"]
    process = subprocess.Popen(
        [server["command"], *server["args"]],
        cwd=server["cwd"],
        env={**os.environ, **server["env"]},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return BuiltinMCPServerHandle(process=process)


__all__ = [
    "BuiltinMCPServerHandle",
    "builtin_mcp_server_config",
    "builtin_mcp_server_command",
    "start_builtin_mcp_server",
]
