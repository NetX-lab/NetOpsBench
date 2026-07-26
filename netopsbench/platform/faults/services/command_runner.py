"""Command execution helpers for fault injection."""

from __future__ import annotations

import subprocess

from netopsbench.platform.utils.proc import docker_prefix, safe_run


class CommandRunner:
    """Executes shell and docker commands for fault injection."""

    @staticmethod
    def _error_result(
        args: list[str],
        *,
        returncode: int,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
    ) -> subprocess.CompletedProcess:
        def as_text(value: str | bytes | None) -> str:
            if isinstance(value, bytes):
                return value.decode(errors="replace")
            return value or ""

        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=as_text(stdout),
            stderr=as_text(stderr),
        )

    def run_cmd(
        self,
        args: list[str],
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            return safe_run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return self._error_result(
                args,
                returncode=124,
                stdout=exc.stdout,
                stderr=exc.stderr or f"command timed out after {timeout}s",
            )
        except OSError as exc:
            return self._error_result(
                args,
                returncode=127,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            )

    def docker_exec(self, container: str, cmd_args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return self.run_cmd([*docker_prefix(), "docker", "exec", container] + cmd_args, timeout)

    def container_is_running(self, container: str) -> bool | None:
        result = self.run_cmd(
            [*docker_prefix(), "docker", "inspect", "-f", "{{.State.Running}}", container], timeout=10
        )
        if result.returncode != 0:
            return None
        state = (result.stdout or "").strip().lower()
        if state == "true":
            return True
        if state == "false":
            return False
        return None
