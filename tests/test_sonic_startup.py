"""The retained wrapper preserves sequential upstream startup and optional service handling."""

import subprocess

import pytest

from netopsbench.platform.topology.config import SONIC_START_WRAPPER_SOURCE
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.topology.topology_utils import load_topology_manifest


def test_sonic_service_order_and_renderer_have_no_parallel_helper(tmp_path):
    subprocess.run(["bash", "-n", str(SONIC_START_WRAPPER_SOURCE)], check=True)
    script = SONIC_START_WRAPPER_SOURCE.read_text()
    starts = [
        line.removeprefix("supervisorctl start ")
        for line in script.splitlines()
        if line.startswith("supervisorctl start ")
    ]
    assert starts[starts.index("syncd") : starts.index("syncd") + 3] == ["syncd", "portsyncd", "orchagent"]
    for prerequisite, dependent in [
        ("teamsyncd", "teammgrd"),
        ("fpmsyncd", "zebra"),
        ("zebra", "staticd"),
        ("natmgrd", "natsyncd"),
    ]:
        assert starts.index(prerequisite) < starts.index(dependent)
    assert "nb-start-group" not in script
    generate_topology("xs", str(tmp_path))
    # Do not advertise fast failure detection that the generated FRR config does not enable.
    assert load_topology_manifest(tmp_path).routing.bfd is False
    assert not (tmp_path / "configs/sonic/start_group.py").exists()
    assert "start_group" not in next(tmp_path.glob("*.clab.yaml")).read_text()


@pytest.mark.parametrize("status", [0, 3, 1])
def test_optional_service_absence_is_distinct_from_failure(tmp_path, status):
    script = SONIC_START_WRAPPER_SOURCE.read_text()
    block = script[script.index("if python3 - <<'CHECK_REBOOTBACKEND'") : script.index("# Start arp_update")]
    (tmp_path / "python3").write_text(f"#!/bin/sh\nexit {status}\n")
    (tmp_path / "supervisorctl").write_text("#!/bin/sh\necho started\n")
    for file in tmp_path.iterdir():
        file.chmod(0o755)
    import os

    result = subprocess.run(
        ["bash", "-e", "-c", block],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if status == 1 else 0)
    assert ("started" in result.stdout) == (status == 0)
    assert ("optional rebootbackend is absent" in result.stdout) == (status == 3)
