"""Installed-wheel smoke coverage for packaged runtime resources."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_installed_wheel_generates_topology_without_source_checkout(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(repo / "netopsbench", source / "netopsbench", ignore=shutil.ignore_patterns("__pycache__", "_bin"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (repo / name).exists():
            shutil.copy2(repo / name, source / name)
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(wheel_dir)],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("netopsbench-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "netopsbench/platform/topology/sonic_start.sh" in names
    assert "netopsbench/platform/topology/sonic_vs_base_config_db.json" in names
    assert "netopsbench/platform/observability/assets/telegraf.conf.template" in names
    assert "netopsbench/platform/observability/assets/grafana/provisioning/dashboards/default.yaml" in names
    assert "netopsbench/platform/observability/assets/grafana/provisioning/datasources/default.yaml" in names
    assert "netopsbench/platform/scenario/specs/fault_campaign.yaml" in names
    assert "netopsbench/models/scale_profiles.yaml" in names
    for removed_module in ("contracts", "engine", "payloads", "service"):
        assert f"netopsbench/platform/simulator/{removed_module}.py" not in names
    assert "netopsbench/sdk/simulator_service.py" not in names
    assert not any("rl_experiment" in name or "verl" in name for name in names)
    assert not any(name.startswith("netopsbench/resources/") for name in names)
    assert "netopsbench/platform/observability/assets/telegraf.conf" not in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)

    installed = tmp_path / "installed"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(installed), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (installed / "bin" / "netopsbench-simulator").exists()
    assert not (installed / "bin" / "netopsbench-rl").exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    # Load the wheel first and use the test interpreter's installed dependencies.
    script = f"""
import sys
sys.path.insert(0, {str(installed)!r})
from pathlib import Path
from importlib.resources import files
import netopsbench
from netopsbench.platform.observability.lifecycle import observability_asset_root
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.models.profiles import default_scale_registry
from netopsbench.sdk import NetOpsBench, SimulatorConfig

package_file = Path(netopsbench.__file__).resolve()
assert Path({str(installed)!r}).resolve() in package_file.parents, package_file
output = Path.cwd() / "generated"
result = generate_topology("xs", str(output), name="wheel-xs")
assert Path(result["metadata_file"]).is_file()
assert (output / "configs" / "sonic" / "start.sh").is_file()
assert (output / "configs" / "sonic" / "spine1" / "config_db.json").is_file()
assert (observability_asset_root() / "telegraf.conf.template").is_file()
assert files("netopsbench.platform.scenario").joinpath("specs", "fault_campaign.yaml").is_file()
assert default_scale_registry().get("fat-tree-k12").total_clients == 144
"""
    completed = subprocess.run([sys.executable, "-I", "-c", script], cwd=outside, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
