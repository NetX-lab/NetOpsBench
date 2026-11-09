"""Catalog coverage is stable across scale, with auditable planned witnesses."""

import json
from collections import Counter

import pytest
import yaml

from netopsbench.models.profiles import supported_scales
from netopsbench.platform.faults.catalog import FAULT_MECHANISMS, coverage_units
from netopsbench.platform.scenario.generator import _CandidateIndex, generate, load_topology
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario
from netopsbench.platform.topology.generator import generate_topology


@pytest.mark.parametrize("scale", supported_scales())
def test_catalog_has_one_case_per_necessary_unit(tmp_path, scale):
    root = tmp_path / "topology"
    generate_topology(scale, str(root))
    topo = load_topology(scale, str(root))
    out = tmp_path / "cases"
    paths = generate({"fault_catalog_version": 2}, topo, out, 42)
    expected = 4 if scale == "xs" else 108 if topo.manifest.family == "fat-tree" else 64
    assert len(paths) == expected
    rows = [yaml.safe_load(p.read_text()) for p in paths]
    matrix = json.loads((out / "coverage.json").read_text())
    units = coverage_units(topo.manifest.family, validation=scale == "xs")
    assert {r["unit"] for r in matrix["coverage_units"]} == {unit.id for unit in units}
    assert len(matrix["coverage_units"]) == len(units)
    assert matrix["negative_cases"] == (1 if scale == "xs" else 4)
    if scale != "xs":
        assert set(r["episode"]["fault_type"] for r in rows) == set(FAULT_MECHANISMS) | {"none"}
    index = _CandidateIndex(topo, 42)
    planned = set(index.pairs)
    for row, path in zip(rows, paths, strict=True):
        case = parse_scenario_file(path)
        assert validate_scenario(case) == []
        episode = row["episode"]
        assert episode["duration_seconds"] >= topo.manifest.require_pingmesh_plan().settings.complete_window_seconds
        assert episode["duration_seconds"] == max(
            30, topo.manifest.require_pingmesh_plan().settings.complete_window_seconds
        )
        assert episode["stabilization_time"] == 0
        if episode["fault_type"] == "none":
            continue
        assert "misconfig_kind" not in episode.get("parameters", {})
        assert "mtu" not in episode
        p = episode["parameters"]
        assert all((pair["src"], pair["dst"]) in planned for pair in p["affected_candidates"] + p["control_candidates"])
        if episode["fault_type"] in {"link_flapping", "route_flapping"}:
            assert (p["down_time"], p["up_time"]) == (10, 10)
        if len(FAULT_MECHANISMS[episode["fault_type"]]) > 1:
            assert p["mechanism"] in FAULT_MECHANISMS[episode["fault_type"]]
        if episode["fault_type"] == "ecmp_misconfig":
            assert all(
                not index.contains(pair["src"], pair["dst"], episode["target_device"])
                for pair in p["control_candidates"]
            )
    if topo.manifest.family == "fat-tree":
        for role in ("edge", "agg"):
            assert {r["pod"] for r in matrix["coverage_units"] if r["role"] == role} == set(
                range(1, topo.manifest.facts.num_pods + 1)
            )
        if scale == "fat-tree-k18":
            for role in ("edge", "agg"):
                assert any(int(r["root"][len(role) :]) > 155 for r in matrix["coverage_units"] if r["role"] == role)
            assert any(int(r["root"][4:]) > 60 for r in matrix["coverage_units"] if r["role"] == "core")
    else:
        assert not any(r["mechanism"] == "multipath_relax_disabled" for r in matrix["coverage_units"])
    first = {p.name: p.read_bytes() for p in paths}
    second = generate({"fault_catalog_version": 2}, topo, out, 42)
    assert first == {p.name: p.read_bytes() for p in second}


def test_catalog_totals_and_mechanisms():
    assert len(FAULT_MECHANISMS) == 24
    assert sum(map(len, FAULT_MECHANISMS.values())) == 27
    assert len(coverage_units("clos")) == 60
    assert len(coverage_units("fat-tree")) == 104
    assert 3 * 64 + 3 * 108 == 516
    assert Counter(u.fault_type for u in coverage_units("fat-tree"))["route_filter_misconfig"] == 9


@pytest.mark.parametrize("fault", ["packet_loss", "acl_source_misconfig", "acl_l4_port_misconfig"])
def test_core_dataplane_effect_audits_use_all_matching_existing_pairs(tmp_path, fault):
    root = tmp_path / "topology"
    generate_topology("fat-tree-k8", str(root))
    index = _CandidateIndex(load_topology("fat-tree-k8", str(root)), 42)
    unit = next(u for u in coverage_units("fat-tree") if u.fault_type == fault and u.role == "core")
    placement = index.place(unit)
    source = placement.witnesses[0]["src"]
    expected = set()
    for src, dst in index.pairs:
        forward = index.contains(src, dst, placement.device, placement.peer)
        backward = index.contains(dst, src, placement.device, placement.peer)
        if fault == "acl_source_misconfig":
            matching = src == source and forward or dst == source and backward
        elif fault == "acl_l4_port_misconfig":
            matching = forward
        else:
            matching = forward or backward
        if matching:
            expected.add((src, dst))
    actual = {(pair["src"], pair["dst"]) for pair in placement.witnesses}
    assert actual == expected
    assert len(actual) > (1 if fault == "acl_source_misconfig" else 8)


def test_old_campaign_is_rejected(tmp_path):
    root = tmp_path / "topology"
    generate_topology("xs", str(root))
    with pytest.raises(ValueError, match="Obsolete campaign"):
        generate({"fault_templates": []}, load_topology("xs", str(root)), tmp_path / "cases", 1)
