"""Public SDK helpers for the built-in NetOpsBench MCP server."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MCP_ENV_ALLOWLIST = frozenset(
    {
        "NETOPSBENCH_INFLUXDB_BUCKET",
        "NETOPSBENCH_INFLUXDB_ORG",
        "NETOPSBENCH_INFLUXDB_TOKEN",
        "NETOPSBENCH_INFLUXDB_URL",
        "NETOPSBENCH_LOG_LEVEL",
        "NETOPSBENCH_NO_SUDO",
        "NETOPSBENCH_PINGMESH_CONTEXT_FILE",
        "NETOPSBENCH_PINGMESH_END_TIME",
        "NETOPSBENCH_PINGMESH_START_TIME",
        "NETOPSBENCH_TOPOLOGY_DIR",
    }
)
_PROCESS_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
    }
)


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

    resolved_workspace = Path(workspace).resolve()
    passthrough_env = {key: os.environ[key] for key in _MCP_ENV_ALLOWLIST if os.environ.get(key)}
    if env:
        passthrough_env.update(
            {key: value for key, value in env.items() if key in _MCP_ENV_ALLOWLIST and isinstance(value, str) and value}
        )

    return {
        "netopsbench": {
            "transport": "stdio",
            "command": python_executable or sys.executable,
            "args": ["-m", "netopsbench.platform.toolkit.fastmcp_server"],
            "cwd": str(resolved_workspace),
            "env": passthrough_env,
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
    process_env = {key: os.environ[key] for key in _PROCESS_ENV_ALLOWLIST if os.environ.get(key)}
    process = subprocess.Popen(
        [server["command"], *server["args"]],
        cwd=server["cwd"],
        env={**process_env, **server["env"]},
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
