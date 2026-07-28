"""Tests for :func:`netopsbench.platform.utils.proc.safe_run`."""

from __future__ import annotations

import logging  # noqa: F401  — kept in case future tests need direct logger access
import subprocess

import pytest

from netopsbench.platform.utils import proc
from netopsbench.platform.utils.proc import (
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    docker_prefix,
    safe_run,
    sudo_prefix,
)


def test_safe_run_returns_completed_process_for_zero_exit():
    result = safe_run(["true"])
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0


def test_safe_run_does_not_raise_on_nonzero_by_default():
    result = safe_run(["false"])
    assert result.returncode != 0


def test_safe_run_check_true_raises_on_nonzero():
    with pytest.raises(subprocess.CalledProcessError):
        safe_run(["false"], check=True)


def test_safe_run_captures_output_as_text_by_default():
    result = safe_run(["printf", "hello"])
    assert result.stdout == "hello"
    assert result.returncode == 0


def test_safe_run_timeout_raises():
    with pytest.raises(subprocess.TimeoutExpired):
        safe_run(["sleep", "5"], timeout=0.1)


def test_safe_run_timeout_redacts_secret_assignments():
    rendered = proc._redacted_command(["sh", "-c", "API_TOKEN=super-secret OTHER_SECRET='secret with spaces' sleep 5"])

    assert "super-secret" not in rendered
    assert "secret with spaces" not in rendered
    assert "API_TOKEN=<redacted>" in rendered
    assert "OTHER_SECRET=<redacted>" in rendered


def test_safe_run_accepts_none_timeout():
    # Just ensure passing timeout=None is wired through; use a fast command.
    result = safe_run(["true"], timeout=None)
    assert result.returncode == 0


def test_default_timeout_constant_is_positive():
    assert isinstance(DEFAULT_SUBPROCESS_TIMEOUT_SECONDS, (int, float))
    assert DEFAULT_SUBPROCESS_TIMEOUT_SECONDS > 0


def test_sudo_prefix_default(monkeypatch):
    monkeypatch.delenv("NETOPSBENCH_NO_SUDO", raising=False)
    assert sudo_prefix() == ["sudo", "-n"]


def test_sudo_prefix_disabled_via_env(monkeypatch):
    monkeypatch.setenv("NETOPSBENCH_NO_SUDO", "1")
    assert sudo_prefix() == []


def test_docker_prefix_uses_accessible_local_socket_without_sudo(monkeypatch):
    monkeypatch.delenv("NETOPSBENCH_NO_SUDO", raising=False)
    monkeypatch.setattr(proc.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(proc.Path, "exists", lambda self: True)
    monkeypatch.setattr(proc.os, "access", lambda path, mode: True)

    assert docker_prefix() == []


def test_docker_prefix_uses_sudo_when_socket_is_not_accessible(monkeypatch):
    monkeypatch.delenv("NETOPSBENCH_NO_SUDO", raising=False)
    monkeypatch.setattr(proc.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(proc.Path, "exists", lambda self: True)
    monkeypatch.setattr(proc.os, "access", lambda path, mode: False)

    assert docker_prefix() == ["sudo", "-n"]
