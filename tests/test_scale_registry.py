"""Scale registry contracts for built-in and user-defined profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from netopsbench.models.profiles import ScaleRegistry
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.sdk import NetOpsBench


def _write_profile(path: Path, *, name: str = "clos-lab", override: bool = False, leafs: int = 3) -> Path:
    path.write_text(
        f"""schema_version: \"1\"
override: {str(override).lower()}
profiles:
  - name: {name}
    topology: {{family: clos, spines: 3, leafs: {leafs}, clients_per_leaf: 1}}
    management: {{prefix: 24, subnet_base: 230}}
    pingmesh: {{destination_batch_size: null, rtt_port_pool_size: 8, rtt_ports_per_cycle: 4, cycle_interval_seconds: 1}}
    traffic: {{max_pps_per_client: 75}}
    runtime: {{deploy_timeout_seconds: 1200, worker_deploy_parallelism: 1, health_timeout_seconds: 90}}
""",
        encoding="utf-8",
    )
    return path


def test_custom_same_family_scale_generates_without_code_changes(tmp_path: Path):
    registry = ScaleRegistry.with_builtins([_write_profile(tmp_path / "scale.yaml")])

    result = generate_topology("clos-lab", str(tmp_path / "topology"), scale_registry=registry)
    manifest = json.loads(Path(result["metadata_file"]).read_text(encoding="utf-8"))

    assert registry.get("clos-lab").total_switches == 6
    assert manifest["scale"] == "clos-lab"
    assert manifest["facts"]["num_spines"] == 3
    assert manifest["facts"]["num_leafs"] == 3
    assert manifest["pingmesh"]["rtt_port_pool_size"] == 8
    rendered = yaml.safe_load(Path(result["yaml_file"]).read_text(encoding="utf-8"))
    assert rendered["topology"]["kinds"]["sonic-vs"]["cmd"] == "-c \"trap 'exit 0' TERM INT; sleep infinity & wait $!\""


def test_profile_override_must_be_explicit(tmp_path: Path):
    profile_file = _write_profile(tmp_path / "scale.yaml", name="small")
    with pytest.raises(ValueError, match="override: true"):
        ScaleRegistry.with_builtins([profile_file])

    registry = ScaleRegistry.with_builtins([_write_profile(profile_file, name="small", override=True, leafs=5)])
    assert registry.get("small").num_leafs == 5


def test_registry_digest_is_stable_and_changes_with_profile(tmp_path: Path):
    first = ScaleRegistry.with_builtins([_write_profile(tmp_path / "first.yaml")])
    same = ScaleRegistry.with_builtins([_write_profile(tmp_path / "same.yaml")])
    changed = ScaleRegistry.with_builtins([_write_profile(tmp_path / "changed.yaml", leafs=4)])

    assert first.digest == same.digest
    assert first.digest != changed.digest


def test_bench_owns_one_shared_scale_registry(tmp_path: Path):
    profile_file = _write_profile(tmp_path / "scale.yaml")
    bench = NetOpsBench(workspace=tmp_path, scale_profiles=[profile_file])

    assert "clos-lab" in bench.scales.names()
    assert bench.scenarios.scale_registry is bench.scales.registry
    assert bench.runtimes.scale_registry is bench.scales.registry
    assert bench.simulators.scale_registry is bench.scales.registry
    bench.close()


def test_fat_tree_switch_counts_are_derived_from_k():
    registry = ScaleRegistry.with_builtins()

    k8 = registry.get("fat-tree-k8")
    k12 = registry.get("fat-tree-k12")

    assert (k8.num_cores, k8.num_aggs, k8.num_edges, k8.total_clients) == (16, 32, 32, 128)
    assert (k12.num_cores, k12.num_aggs, k12.num_edges, k12.total_clients) == (36, 72, 72, 144)


def test_large_limits_containerlab_deploy_parallelism():
    profile = ScaleRegistry.with_builtins().get("large")

    assert profile.containerlab_max_workers == 16
