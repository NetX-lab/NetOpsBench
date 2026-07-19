"""Installed-wheel smoke coverage for packaged runtime resources."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path


def test_installed_wheel_generates_topology_without_source_checkout(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(wheel_dir)],
        cwd=repo,
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
    assert not any("rl_experiment" in name or "verl" in name for name in names)
    assert not any(name.startswith("netopsbench/resources/") for name in names)
    assert "netopsbench/platform/observability/assets/telegraf.conf" not in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(venv_dir / "bin" / "netopsbench-simulator"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (venv_dir / "bin" / "netopsbench-rl").exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    script = f"""
from pathlib import Path
from importlib.resources import files
import netopsbench
from netopsbench.platform.observability.lifecycle import observability_asset_root
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.models.profiles import default_scale_registry

source_root = Path({str(repo)!r}).resolve()
package_file = Path(netopsbench.__file__).resolve()
assert source_root not in package_file.parents, package_file
output = Path.cwd() / "generated"
result = generate_topology("xs", str(output), name="wheel-xs")
assert Path(result["metadata_file"]).is_file()
assert (output / "configs" / "sonic" / "start.sh").is_file()
assert (output / "configs" / "sonic" / "spine1" / "config_db.json").is_file()
assert (observability_asset_root() / "telegraf.conf.template").is_file()
assert files("netopsbench.platform.scenario").joinpath("specs", "fault_campaign.yaml").is_file()
assert default_scale_registry().get("fat-tree-k12").total_clients == 144
print(package_file)
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-c", script],
        cwd=outside,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert str(repo) not in completed.stdout
