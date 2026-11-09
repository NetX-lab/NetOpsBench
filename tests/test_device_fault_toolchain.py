"""Device parking must use the same validated toolchain as deployment."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.platform.faults.handlers import system


@pytest.mark.parametrize("operation", ["stop", "start"])
def test_device_lifecycle_uses_pinned_toolchain(monkeypatch, operation):
    pinned = Path("/validated/runtime-tools")
    monkeypatch.setattr(system, "runtime_tools", lambda: pinned)
    monkeypatch.setattr(system, "sudo_prefix", lambda: [])
    handler = system.SystemHandler(None, None, None, SimpleNamespace(topology_file=Path("/runtime/lab.clab.yaml")))

    command = handler._containerlab_node_command(operation, "agg46")

    assert command[0] == str(pinned / "containerlab")
    assert command[1] == operation
    assert command[command.index("--node") + 1] == "agg46"
