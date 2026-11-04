"""Reproducible new cases, with legacy explicit parameters left intact."""

import inspect
import ipaddress

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.faults.builtin.impairment_specs import build_impairment_fault_specs
from netopsbench.platform.faults.defaults import IMPAIRMENT_PARAMETERS
from netopsbench.platform.faults.handlers.impairment import ImpairmentHandler
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.scenario.generator import generate, load_topology
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.topology.generator import generate_topology


def test_fault_entrypoints_share_defaults_and_explicit_values():
    calls = []

    class Injector:
        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append(kwargs) or {}

    for spec in build_impairment_fault_specs():
        defaults = IMPAIRMENT_PARAMETERS[spec.name]
        key, default = next(iter(defaults.items()))
        spec.inject_episode(
            Injector(),
            EpisodeSpec(episode_id="a", fault_type=spec.name, target_device="leaf1", target_interface="Ethernet0"),
        )
        assert calls[-1] == defaults
        spec.inject_episode(
            Injector(),
            EpisodeSpec(
                episode_id="a",
                fault_type=spec.name,
                target_device="leaf1",
                target_interface="Ethernet0",
                parameters={key: 7},
            ),
        )
        assert calls[-1] == {key: 7}
        assert inspect.signature(getattr(ImpairmentHandler, f"inject_{spec.name}")).parameters[key].default == default
        if key != "mtu":
            assert inspect.signature(getattr(FaultInjector, f"inject_{spec.name}")).parameters[key].default == default


def test_new_generated_values_are_reproducible_and_constrained(tmp_path):
    topology = tmp_path / "topology"
    generate_topology("medium", str(topology), name="generation-contract")
    topo = load_topology("medium", str(topology))
    outputs = []
    for label, seed in (("first", 42), ("same", 42), ("other", 43)):
        paths = generate({"fault_catalog_version": 2}, topo, tmp_path / label, seed)
        cases = [parse_scenario_file(path) for path in paths]
        assert all(case.metadata["generator"] == {"seed": seed, "version": 3} for case in cases)
        outputs.append({case.id: case.model_dump(mode="json") for case in cases})
        for case in cases:
            p = case.episode.parameters
            if case.episode.fault_type == "interface_ip_misconfig":
                assert ipaddress.ip_interface(p["fault_address"]).ip in ipaddress.ip_network("198.51.100.0/24")
            if case.episode.fault_type == "bgp_update_source_mismatch":
                assert p["fault_update_source"].startswith("Loopback")
                assert 1 <= int(p["fault_update_source"][8:]) < 4096
    assert outputs[0] == outputs[1]
    assert outputs[0] != outputs[2]
