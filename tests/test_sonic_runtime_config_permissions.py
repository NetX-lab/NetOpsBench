"""Saved FRR configuration remains readable for runtime baseline identity."""

import subprocess
from types import SimpleNamespace

import pytest

from netopsbench.platform.faults.services.sonic_runtime import SonicRuntime


@pytest.mark.parametrize("save", [True, False])
def test_saved_config_keeps_host_readability_without_changing_read_commands(tmp_path, save):
    config = tmp_path / "frr.conf"
    config.write_text("router bgp 65001\n")
    config.chmod(0o640)

    def docker_exec(container, command):
        assert container == "clab-test-edge160"
        if command[0] == "chmod":
            subprocess.run([*command[:2], str(config)], check=True)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    runtime = SonicRuntime(
        SimpleNamespace(docker_exec=docker_exec), SimpleNamespace(container_names={"edge160": "clab-test-edge160"})
    )
    result = runtime.vtysh("edge160", ["write memory"] if save else ["show ip bgp summary"])

    assert result.returncode == 0
    assert bool(config.stat().st_mode & 0o004) is save
    assert config.read_text() == "router bgp 65001\n"


def test_saved_config_permission_failure_is_reported():
    def docker_exec(container, command):
        return subprocess.CompletedProcess(command, 1 if command[0] == "chmod" else 0, "", "read-only mount")

    runtime = SonicRuntime(
        SimpleNamespace(docker_exec=docker_exec), SimpleNamespace(container_names={"edge160": "clab-test-edge160"})
    )
    result = runtime.vtysh("edge160", ["write memory"])

    assert result.returncode == 1
    assert result.stderr == "read-only mount"
