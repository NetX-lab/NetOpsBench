"""Process execution utilities shared across platform modules."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence

logger = logging.getLogger(__name__)


# Default subprocess timeout (seconds) for short-lived shell calls. Long-running
# operations (lab deploy, telegraf install) should pass an explicit larger
# timeout or ``None``.
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120


def sudo_prefix() -> list[str]:
    """Return the sudo prefix for privileged subprocess calls.

    By default returns ``["sudo", "-n"]`` so that Docker and Containerlab
    commands work on machines where the user is not in the ``docker`` group.

    Set the environment variable ``NETOPSBENCH_NO_SUDO=1`` to suppress the
    prefix (e.g. in CI containers where the process already runs as root or
    in a docker-group session).
    """
    if os.environ.get("NETOPSBENCH_NO_SUDO", "").strip() == "1":
        return []
    return ["sudo", "-n"]


def safe_run(
    cmd: Sequence[str],
    *,
    timeout: float | None = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    cwd: str | None = None,
    env: dict | None = None,
    input: str | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.CompletedProcess:
    """Run a subprocess with a sane default timeout and structured logging.

    Wraps :func:`subprocess.run` so that platform code never silently hangs
    on stuck Docker / Containerlab / vtysh invocations.

    Args:
        cmd: The command list to execute (no ``shell=True``).
        timeout: Maximum seconds to wait. ``None`` disables the timeout
            (use only for known-long deploy operations). Defaults to
            :data:`DEFAULT_SUBPROCESS_TIMEOUT_SECONDS`.
        check: If True, raise :class:`subprocess.CalledProcessError` on
            non-zero exit. Defaults to False so callers can inspect
            ``result.returncode`` themselves.
        capture_output: Capture stdout/stderr (default True). Ignored when
            ``stdout`` or ``stderr`` are provided explicitly.
        text: Decode output as text (default True).
        cwd: Optional working directory.
        env: Optional environment override.
        input: Optional stdin payload.
        stdout: Optional file-like object to redirect stdout to. When set,
            ``capture_output`` is ignored for stdout.
        stderr: Optional file-like object to redirect stderr to. When set,
            ``capture_output`` is ignored for stderr.

    Returns:
        The :class:`subprocess.CompletedProcess`.

    Raises:
        subprocess.TimeoutExpired: If ``timeout`` is exceeded. The exception
            is logged at ERROR level with the command for triage.
        subprocess.CalledProcessError: If ``check=True`` and exit code is
            non-zero.
    """
    cmd_list = list(cmd)
    # When explicit stdout/stderr file handles are provided, capture_output
    # must not be set (they are mutually exclusive in subprocess.run).
    if stdout is not None or stderr is not None:
        run_kwargs: dict = dict(
            timeout=timeout,
            check=check,
            text=text,
            cwd=cwd,
            env=env,
            input=input,
        )
        if stdout is not None:
            run_kwargs["stdout"] = stdout
        if stderr is not None:
            run_kwargs["stderr"] = stderr
    else:
        run_kwargs = dict(
            timeout=timeout,
            check=check,
            capture_output=capture_output,
            text=text,
            cwd=cwd,
            env=env,
            input=input,
        )
    try:
        return subprocess.run(cmd_list, **run_kwargs)
    except subprocess.TimeoutExpired:
        logger.error(
            "subprocess timed out after %ss: %s",
            timeout,
            " ".join(str(part) for part in cmd_list),
        )
        raise
