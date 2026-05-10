"""Command execution helpers for fault injection."""

from __future__ import annotations

import os
import signal
import subprocess

from netopsbench.platform.utils.proc import safe_run, sudo_prefix


class CommandRunner:
    """Executes shell and docker commands for fault injection."""

    def run_cmd(
        self,
        args: list[str],
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        return safe_run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def terminate_process(self, pid: int | None, *, sig: int = signal.SIGKILL) -> bool:
        if pid in (None, ""):
            return True
        try:
            os.kill(int(pid), sig)
            return True
        except ProcessLookupError:
            return True
        except (OSError, TypeError, ValueError):
            return False

    def docker_exec(self, container: str, cmd_args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return self.run_cmd([*sudo_prefix(), "docker", "exec", container] + cmd_args, timeout)

    def docker_exec_detached(
        self, container: str, cmd_args: list[str], timeout: int = 10
    ) -> subprocess.CompletedProcess:
        return self.run_cmd([*sudo_prefix(), "docker", "exec", "-d", container] + cmd_args, timeout)

    def container_is_running(self, container: str) -> bool:
        result = self.run_cmd([*sudo_prefix(), "docker", "inspect", "-f", "{{.State.Running}}", container], timeout=10)
        return result.returncode == 0 and (result.stdout or "").strip() == "true"
