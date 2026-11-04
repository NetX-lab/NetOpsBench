"""Configuration ownership, preconditions, and private/public evidence boundaries."""

import json
import subprocess

import pytest

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.faults.catalog import FAULT_MECHANISMS
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.services.periodic import PeriodicController
from netopsbench.platform.faults.specs import create_fault_registry
from netopsbench.platform.observability.bgp_collector import BgpTransitionTracker
from netopsbench.platform.scenario.effects import scoped_integrity_errors
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import config_for_scale


@pytest.fixture
def injector():
    return FaultInjector(topology_metadata=build_clos_plan(config_for_scale("medium")).manifest.model_dump(mode="json"))


def test_registry_is_the_complete_catalog_and_old_types_are_absent():
    registry = create_fault_registry()
    assert set(registry.supported_scenario_faults()) == set(FAULT_MECHANISMS)
    for retired in ("bgp_neighbor_misconfig", "route_policy_misconfig", "acl_misconfig", "acl_misconfiguration"):
        assert registry.get(retired) is None


@pytest.mark.parametrize("fault", ("route_origination_misconfig", "route_filter_misconfig", "ecmp_misconfig"))
def test_mechanism_is_required_in_parameters(fault):
    spec = create_fault_registry().get(fault)
    episode = EpisodeSpec(
        episode_id="a",
        fault_type=fault,
        target_device="leaf1",
        target_prefix="192.168.2.0/30",
        metadata={"mechanism": FAULT_MECHANISMS[fault][0]},
    )
    assert any("mechanism" in error for error in spec.validate_episode(episode))
    episode = episode.model_copy(
        update={"parameters": {"mechanism": FAULT_MECHANISMS[fault][0], "peer_ip": "10.0.0.1"}}
    )
    assert not any("mechanism" in error for error in spec.validate_episode(episode))


def test_malformed_acl_direction_returns_a_validation_error():
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="acl_l4_port_misconfig",
        target_device="leaf1",
        target_interface="Ethernet0",
        target_prefix="192.168.2.0/30",
        parameters={"direction": {"out": True}},
    )
    errors = create_fault_registry().get(episode.fault_type).validate_episode(episode)
    assert "ACL direction must be in or out" in errors


def test_partial_transaction_compensates_every_attempted_write(injector, monkeypatch):
    handler = injector._network
    state, attempts = {}, []
    operations = [
        {"kind": "docker", "apply": ["set", "one"], "restore": ["undo", "one"]},
        {"kind": "docker", "apply": ["set", "two"], "restore": ["undo", "two"]},
    ]
    monkeypatch.setattr(handler, "_prepare", lambda ep: (operations, {}))

    def run(device, kind, args):
        attempts.append(args)
        if args[0] == "set":
            state[args[1]] = True
            if args[1] == "two":
                raise RuntimeError("partial write")
        else:
            state.pop(args[1], None)
        return ""

    monkeypatch.setattr(handler, "_run", run)
    result = handler.inject(EpisodeSpec(episode_id="a", fault_type="bgp_neighbor_shutdown", target_device="leaf1"))
    assert not result["success"]
    assert state == {}
    assert attempts[-2:] == [["undo", "two"], ["undo", "one"]]
    assert not injector.active_faults


def test_failed_compensation_tracks_residual_and_keeps_other_cleanup(injector, monkeypatch):
    handler = injector._network
    called = []

    def run(device, kind, args):
        called.append(args)
        if args == ["two"]:
            raise RuntimeError("cannot restore two")

    monkeypatch.setattr(handler, "_run", run)
    result = handler.recover(
        {
            "type": "route_filter_misconfig",
            "device": "leaf1",
            "journal": [{"kind": "docker", "restore": ["one"]}, {"kind": "docker", "restore": ["two"]}],
        }
    )
    assert not result["recovered"]
    assert called == [["two"], ["one"]]


def test_policy_is_cloned_and_original_attachment_restored(injector, monkeypatch):
    handler = injector._network
    monkeypatch.setattr(handler, "_run", lambda *args: "route-map RM-ALLOW permit 10\n set metric 7\n!\n")
    monkeypatch.setattr(
        handler.routing,
        "get_bgp_config_snapshot",
        lambda device: {
            "neighbors": [{"peer_ip": "10.0.0.1", "route_maps": [{"name": "RM-ALLOW", "direction": "out"}]}]
        },
    )
    operations = handler._prefix_policy("leaf1", "192.168.1.0/30", ["10.0.0.1"], "out")
    write = "\n".join(operations[0]["apply"])
    assert "route-map RM-ALLOW permit" not in write
    assert "set metric 7" in write
    assert "deny 1" in write
    assert "neighbor 10.0.0.1 route-map RM-ALLOW out" in operations[0]["restore"]
    assert not any(command == "no route-map RM-ALLOW" for command in operations[0]["restore"])


def test_ecmp_rejects_single_actual_next_hop_before_mutating(injector, monkeypatch):
    handler = injector._network
    monkeypatch.setattr(handler, "_run", lambda *args: "")
    monkeypatch.setattr(handler, "_json", lambda *args: [{"dst": "192.168.2.0/30", "gateway": "10.0.0.1"}])
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="ecmp_misconfig",
        target_device="leaf1",
        target_prefix="192.168.2.0/30",
        parameters={"mechanism": "max_paths_limit"},
    )
    with pytest.raises(RuntimeError, match="actual kernel FIB"):
        handler._prepare(episode)
    assert not injector.active_faults


@pytest.mark.parametrize(
    "second_path, eligible",
    [
        ({}, True),
        ({"aspath": {"string": "65033 65067"}}, False),
        ({"aspath": {}}, False),
        ({"aspath": {"string": ""}}, False),
        ({"aspath": {"string": 65034}}, False),
        ({"valid": False}, False),
        ({"multipath": False}, False),
        ({"nexthops": [{"ip": "10.2.1.21", "accessible": False, "used": True}]}, False),
        ({"nexthops": [{"ip": "10.2.1.99", "accessible": True, "used": True}]}, False),
    ],
)
def test_multipath_relax_requires_distinct_installed_frr_as_paths(injector, monkeypatch, second_path, eligible):
    handler = injector._network
    reads = []

    def read_config(device, kind, commands):
        reads.append(commands)
        assert commands == ["show running-config"]
        return "router bgp 65011\n bgp bestpath as-path multipath-relax\n"

    def path(gateway, as_path):
        return {
            "valid": True,
            "multipath": True,
            "aspath": {"string": as_path},
            "nexthops": [{"ip": gateway, "accessible": True, "used": True}],
        }

    first = path("10.2.1.5", "65033 65067")
    second = {**path("10.2.1.21", "65034 65067"), **second_path}
    monkeypatch.setattr(handler, "_run", read_config)
    monkeypatch.setattr(
        handler,
        "_json",
        lambda *a: [{"nexthops": [{"gateway": "10.2.1.5"}, {"gateway": "10.2.1.21"}]}],
    )
    monkeypatch.setattr(handler, "_frr_json", lambda *a: {"paths": [first, second]})
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="ecmp_misconfig",
        target_device="leaf1",
        target_prefix="192.168.19.4/30",
        parameters={"mechanism": "multipath_relax_disabled"},
    )
    if eligible:
        operations, evidence = handler._prepare(episode)
        assert "no bgp bestpath as-path multipath-relax" in operations[0]["apply"]
        assert "bgp bestpath as-path multipath-relax" in operations[0]["restore"]
        assert evidence["healthy_bgp"]["paths"] == [first, second]
    else:
        with pytest.raises(RuntimeError, match="distinct valid AS_PATHs"):
            handler._prepare(episode)
    assert reads == [["show running-config"]]
    assert not injector.active_faults


@pytest.mark.parametrize("received", [1, 2, 14])
def test_maximum_prefix_uses_healthy_neighbor_count_and_supported_json_command(injector, monkeypatch, received):
    handler = injector._network
    reads = []

    def run(device, kind, commands):
        reads.append(commands)
        if commands == ["show running-config"]:
            return "router bgp 65011\n neighbor 10.1.1.1 remote-as 65001\n"
        assert commands == ["show ip bgp neighbors 10.1.1.1 json"]
        return json.dumps({"10.1.1.1": {"addressFamilyInfo": {"ipv4Unicast": {"acceptedPrefixCounter": received}}}})

    monkeypatch.setattr(handler, "_run", run)
    monkeypatch.setattr(handler, "_neighbor", lambda *a: {"remote_as": 65001})
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="bgp_max_prefix_misconfig",
        target_device="leaf1",
        parameters={"peer_ip": "10.1.1.1"},
    )
    if received < 2:
        with pytest.raises(RuntimeError, match="at least two healthy"):
            handler._prepare(episode)
    else:
        operations, evidence = handler._prepare(episode)
        assert f"neighbor 10.1.1.1 maximum-prefix {received - 1}" in operations[0]["apply"]
        assert evidence["healthy_prefix_count"] == received
    assert all(command[0].startswith("show ") for command in reads)


def test_routing_loop_rejects_an_unhealthy_or_unknown_neighbor_before_mutation(injector, monkeypatch):
    handler = injector._network
    commands = []
    monkeypatch.setattr(handler, "_run", lambda device, kind, args: commands.append(args) or "")
    monkeypatch.setattr(handler.routing, "pick_bgp_neighbor", lambda *a, **kw: None)
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="routing_loop",
        target_device="leaf1",
        target_interface="Ethernet0",
        target_prefix="192.168.2.2/32",
        parameters={"next_hop": "192.0.2.1"},
    )
    with pytest.raises(RuntimeError, match="not readable and established"):
        handler._prepare(episode)
    assert commands == [["show running-config"]]


@pytest.mark.parametrize("readable", [True, False])
def test_bgpd_stop_requires_a_captured_daemon_configuration(injector, monkeypatch, readable):
    handler = injector._network
    original = "!\nrouter bgp 65011\n neighbor 10.1.1.1 remote-as 65001\n!\nend\n"
    commands = []

    def run(device, kind, args):
        commands.append(args)
        if args[0] == "supervisorctl":
            return "bgpd RUNNING pid 123, uptime 10:00"
        return "Building configuration...\n\nCurrent configuration:\n" + original if readable else "% unavailable"

    monkeypatch.setattr(handler, "_run", run)
    episode = EpisodeSpec(episode_id="a", fault_type="bgp_process_down", target_device="leaf1")
    if not readable:
        with pytest.raises(RuntimeError, match="Cannot capture"):
            handler._prepare(episode)
    else:
        operations, evidence = handler._prepare(episode)
        assert evidence["original_bgp_config"] == original
        assert operations[0]["restore"][-1] == original
        assert operations[0]["apply"] == ["supervisorctl", "stop", "bgpd"]
    assert commands == [
        ["supervisorctl", "status", "bgpd"],
        ["vtysh", "-d", "bgpd", "-c", "show running-config"],
    ]


@pytest.mark.parametrize("exists", [False, True])
def test_update_source_uses_an_absent_interface_and_restores_the_original_option(injector, monkeypatch, exists):
    handler = injector._network
    monkeypatch.setattr(handler, "_neighbor", lambda *a: {"remote_as": 65001})
    monkeypatch.setattr(handler, "_run", lambda *a: "router bgp 65011\n neighbor 10.1.1.1 update-source Ethernet0\n")
    monkeypatch.setattr(handler, "_json", lambda *a: [{"ifname": "Loopback999" if exists else "Ethernet0"}])
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="bgp_update_source_mismatch",
        target_device="leaf1",
        parameters={"peer_ip": "10.1.1.1"},
    )
    if exists:
        with pytest.raises(RuntimeError, match="already exists"):
            handler._prepare(episode)
    else:
        operations, evidence = handler._prepare(episode)
        assert "neighbor 10.1.1.1 update-source Loopback999" in operations[0]["apply"]
        assert "neighbor 10.1.1.1 update-source Ethernet0" in operations[0]["restore"]
        assert evidence["fault_update_source"] == "Loopback999"


@pytest.mark.parametrize("running", [True, False])
def test_bgpd_recovery_reloads_original_configuration_even_if_process_is_running(monkeypatch, running):
    from pathlib import Path

    from netopsbench.platform.faults.services import _bgpd_restore

    calls, paths, budgets = [], [], []
    config = "router bgp 65011\n neighbor 10.1.1.1 remote-as 65001\nend\n"
    monkeypatch.setattr(_bgpd_restore.sys, "argv", ["restore", config])
    times = iter([0, 1, 2, 3])
    monkeypatch.setattr(_bgpd_restore.time, "monotonic", lambda: next(times))

    def run(command, **kwargs):
        calls.append(command)
        budgets.append(kwargs["timeout"])
        if command[:2] == ["supervisorctl", "status"]:
            return subprocess.CompletedProcess(command, 0 if running else 3, "RUNNING" if running else "STOPPED", "")
        if command[0] == "vtysh":
            paths.append(Path(command[-1]))
            assert paths[-1].read_text() == config
            assert command[:4] == ["vtysh", "-d", "bgpd", "-f"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_bgpd_restore.subprocess, "run", run)
    _bgpd_restore.restore_bgpd()
    assert (["supervisorctl", "start", "bgpd"] in calls) is (not running)
    assert budgets == sorted(budgets, reverse=True)
    assert not paths[0].exists()


def test_bgpd_recovery_does_not_start_another_command_after_its_deadline(monkeypatch):
    from netopsbench.platform.faults.services import _bgpd_restore

    calls = []
    times = iter([0, 1, 26])
    monkeypatch.setattr(_bgpd_restore.time, "monotonic", lambda: next(times))

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 3, "STOPPED", "")

    monkeypatch.setattr(_bgpd_restore.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="deadline"):
        _bgpd_restore.restore_bgpd()
    assert calls == [["supervisorctl", "status", "bgpd"]]


def test_netem_refuses_unowned_qdisc(injector, monkeypatch):
    monkeypatch.setattr(
        injector._cmd, "docker_exec", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout='[{"kind":"htb"}]')
    )
    with pytest.raises(RuntimeError, match="Non-default qdisc"):
        injector.inject_packet_loss("leaf1", "Ethernet0")
    assert not injector.active_faults


def test_periodic_controller_stops_and_records_verified_phases():
    import time

    states = []
    controller = PeriodicController(
        lambda: states.append("fault"), lambda: states.append("healthy"), down_time=0.01, up_time=0.01
    )
    controller.start()
    time.sleep(0.06)
    controller.stop_event.set()
    controller.thread.join(timeout=1)
    assert not controller.thread.is_alive()
    assert states[:3] == ["fault", "healthy", "fault"]
    assert len(controller.history) >= 3


@pytest.mark.parametrize("failed_phase", ["fault", "healthy"])
def test_periodic_controller_preserves_errors_and_never_records_a_failed_phase(failed_phase):
    calls = []

    def phase(name):
        calls.append(name)
        if name == failed_phase and len(calls) > 1:
            raise RuntimeError()

    controller = PeriodicController(lambda: phase("fault"), lambda: phase("healthy"), down_time=0.001, up_time=0.001)
    controller.start()
    controller.thread.join(timeout=1)
    assert not controller.thread.is_alive()
    assert controller.stop_event.is_set()
    assert controller.error == "RuntimeError: "
    assert [row["phase"] for row in controller.history] == calls[:-1]


def test_prefix_event_does_not_interpret_collection_failure_as_zero():
    tracker = BgpTransitionTracker()
    row = {"neighbor": "10.0.0.1", "state": "Established", "prefixes_received": 8}
    tracker.process("leaf1", [row], 1, "lab", collection_ok=True)
    failure = tracker.process("leaf1", [], 2, "lab", collection_ok=False)
    assert not any("bgp_prefix_events" in line for line in failure)
    changed = tracker.process("leaf1", [dict(row, prefixes_received=6)], 3, "lab", collection_ok=True)
    assert any("prefixes_before=8i,prefixes_after=6i" in line for line in changed)
    assert not any("bgp_session_events" in line for line in changed)


def test_scoped_outage_requires_complete_remaining_tasks():
    obs = {
        "data_source_status": "ok",
        "_baseline_coverage": {"coverage_status": "complete"},
        "coverage_status": "incomplete",
    }
    effect = {"unobservable_clients": ["client1"]}
    audit = {
        "missing_tasks": [["client1", "client2", 0]],
        "invalid_tasks": [],
        "observable_pairs": [["client2", "client3"]],
    }
    assert scoped_integrity_errors(obs, audit, effect) == []
    audit["missing_tasks"].append(["client2", "client3", 0])
    assert scoped_integrity_errors(obs, audit, effect)


def test_periodic_outages_include_verified_transition_time_only():
    from netopsbench.platform.scenario.effects import _periodic_client_outages

    phases = [
        {"phase": "fault", "time": "2026-10-12T12:00:00+00:00"},
        {"phase": "healthy", "started_at": "2026-10-12T12:00:20+00:00", "time": "2026-10-12T12:00:21+00:00"},
        {"phase": "fault", "started_at": "2026-10-12T12:00:41+00:00", "time": "2026-10-12T12:00:42+00:00"},
    ]
    assert _periodic_client_outages(phases, ["client1"], "2026-10-12T12:00:50Z") == [
        {"client": "client1", "start": "2026-10-12T12:00:00+00:00", "end": "2026-10-12T12:00:21+00:00"},
        {"client": "client1", "start": "2026-10-12T12:00:41+00:00", "end": "2026-10-12T12:00:50+00:00"},
    ]


@pytest.mark.parametrize(
    "change,missing,valid",
    [
        ({}, [], True),
        ({"time": "2026-10-12T12:00:21.5Z"}, [], True),
        ({"time": "2026-10-12T12:00:24Z"}, [], False),
        ({"src": "client2", "dst": "client1"}, [], False),
        ({"socket_batch_valid": False}, [], False),
        ({"time": ""}, [], False),
        ({}, [["client1", "client2", 0]], False),
        ({}, [["client2", "client3", 0]], False),
    ],
)
def test_periodic_outage_does_not_excuse_missing_tasks_or_errors_in_healthy_phases(change, missing, valid):
    obs = {"data_source_status": "ok", "_baseline_coverage": {"coverage_status": "complete"}}
    audit = {
        "missing_tasks": missing,
        "invalid_tasks": [["client1", "client2", 0]],
        "invalid_socket_rows": 1,
        "invalid_rows": [
            {
                "src": "client1",
                "dst": "client2",
                "time": "2026-10-12T12:00:10Z",
                "local_probe_errors": 1,
                "socket_batch_valid": True,
                **change,
            }
        ],
        "coverage_status": "incomplete",
        "observable_pairs": [["client1", "client2"], ["client2", "client3"]],
    }
    effect = {
        "bounded_client_outages": [
            {"client": "client1", "start": "2026-10-12T12:00:00+00:00", "end": "2026-10-12T12:00:21+00:00"}
        ],
        "probe_cycle_seconds": 1,
    }
    assert (not scoped_integrity_errors(obs, audit, effect)) is valid
    assert audit["coverage_status"] == "incomplete"
    assert audit["invalid_socket_rows"] == 1


def test_private_probe_evidence_reuses_rows_and_preserves_local_errors():
    from netopsbench.platform.pingmesh._detector_analysis import probe_pair_evidence

    row = {
        "src_name": "client1",
        "dst_name": "client2",
        "packets_sent": 8,
        "packets_lost": 0,
        "rtt_avg": 1,
        "rtt_max": 2,
        "local_probe_errors": 0,
    }
    fault = {**row, "packets_lost": 3, "rtt_max": 122, "local_probe_errors": 1}
    result = probe_pair_evidence(
        [row], [fault, {**fault, "src_name": "unrelated"}], [{"src": "client1", "dst": "client2"}]
    )
    assert len(result) == 1
    assert result[0]["baseline"]["regular"]["lost"] == 0
    assert result[0]["fault"]["regular"]["lost"] == 3
    assert result[0]["fault"]["rtt_max_ms"] == 122
    assert result[0]["fault"]["local_probe_errors"] == 1


def test_candidate_scope_covers_all_planned_flows_instead_of_only_the_anchor():
    from netopsbench.platform.scenario.candidates import CandidatePaths

    manifest = build_clos_plan(config_for_scale("medium")).manifest
    graph = CandidatePaths(manifest)
    anchor = {"src": "client12", "dst": "client10"}
    pairs = graph.affected_pairs("mtu_mismatch", "leaf6", "spine3", anchor)
    assert pairs[0] == anchor
    assert {"src": "client11", "dst": "client3"} in pairs
    assert len(pairs) == 28
    assert all((pair["src"], pair["dst"]) in graph.pairs for pair in pairs)
    assert all(
        graph.contains(p["src"], p["dst"], "leaf6", "spine3") or graph.contains(p["dst"], p["src"], "leaf6", "spine3")
        for p in pairs
    )


def test_effect_audits_scoped_existing_flows_without_changing_case_parameters(monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import validate_effect
    from netopsbench.platform.toolkit._core.common import ToolResult

    manifest = build_clos_plan(config_for_scale("medium")).manifest
    anchor = {"src": "client12", "dst": "client10"}
    affected = {"src": "client11", "dst": "client3"}
    control = {"src": "client2", "dst": "client4"}
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="mtu_mismatch",
        target_device="leaf6",
        target_interface="Ethernet8",
        parameters={"mtu": 1400, "affected_candidates": [anchor], "control_candidates": [control]},
    )
    toolkit = SimpleNamespace(
        get_device_status=lambda device: ToolResult(success=True, data={"running": True, "accessible": True}),
        get_device_interfaces=lambda *args, **kwargs: ToolResult(success=True, data={}),
    )
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    executor = SimpleNamespace(
        current_manifest=lambda: manifest,
        influxdb_bucket="worker",
        topology_id=manifest.topology_id,
        injector=SimpleNamespace(_iface=SimpleNamespace(resolve_sonic=lambda interface: interface)),
        _case_affected_pairs=[anchor, affected],
    )
    probes = [
        {
            **pair,
            "baseline": {"samples": 20, "df": {"lost": 0, "loss_pct": 0}},
            "fault": {"samples": 20, "df": {"lost": 0, "loss_pct": 0}},
        }
        for pair in [anchor, affected, control]
    ]
    probes[1]["fault"]["df"] = {"lost": 16, "loss_pct": 100}
    result = validate_effect(
        executor, episode, {"observations": {"_probe_evidence": probes}}, {"nodes": {}}, {"nodes": {}}
    )
    assert result["valid"]
    assert episode.parameters["affected_candidates"] == [anchor]


@pytest.mark.parametrize(
    ("df_lost", "mtu_drops", "local_errors", "baseline_loss_pct", "valid"),
    [(16, 0, 0, 0, True), (16, 16, 0, 0, False), (16, 0, 1, 0, False), (16, 0, 0, 80, False), (2, 0, 0, 0, False)],
)
def test_acl_effect_accepts_df_path_loss_without_mtu_or_local_errors(
    monkeypatch, df_lost, mtu_drops, local_errors, baseline_loss_pct, valid
):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import validate_effect
    from netopsbench.platform.toolkit._core.common import ToolResult

    manifest = build_clos_plan(config_for_scale("medium")).manifest
    pair = {"src": "client1", "dst": "client3"}
    toolkit = SimpleNamespace(
        get_device_status=lambda device: ToolResult(success=True, data={"running": True, "accessible": True}),
    )
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    injector = SimpleNamespace(
        container_names={"leaf1": "container"},
        _cmd=SimpleNamespace(docker_exec=lambda *args: SimpleNamespace(returncode=0, stdout="36 DROP OWNED")),
    )
    executor = SimpleNamespace(
        current_manifest=lambda: manifest,
        influxdb_bucket="bucket",
        topology_id="topology",
        injector=injector,
        _case_affected_pairs=[pair],
    )
    episode = EpisodeSpec(episode_id="a", fault_type="acl_destination_misconfig", target_device="leaf1", parameters={})
    probes = [
        {
            **pair,
            "baseline": {"samples": 20, "regular": {"loss_pct": 0}, "df": {"loss_pct": baseline_loss_pct}},
            "fault": {
                "samples": 20,
                "local_probe_errors": local_errors,
                "regular": {"lost": 2, "loss_pct": 10},
                "df": {"lost": df_lost, "loss_pct": df_lost * 5, "mtu_drops": mtu_drops},
            },
        }
    ]
    result = validate_effect(
        executor,
        episode,
        {
            "injection": {"evidence": {"acl_name": "OWNED", "rule": ["FORWARD", "-o", "Ethernet0"]}},
            "observations": {"_probe_evidence": probes},
        },
        {"nodes": {}},
        {"nodes": {}},
    )
    assert result["valid"] is valid
    assert result["evidence"]["matched_probe_loss_kinds"] == (["df"] if valid else [])


def test_periodic_policy_recovery_skips_already_removed_owned_objects(injector, monkeypatch):
    handler = injector._network
    calls = []

    def run(device, kind, commands):
        calls.append(commands)
        return (
            "router bgp 65011\n neighbor 10.0.0.1 route-map RM-ALLOW out\n"
            if commands == ["show running-config"]
            else ""
        )

    monkeypatch.setattr(handler, "_run", run)
    result = handler.recover(
        {
            "type": "route_flapping",
            "device": "leaf1",
            "journal": [
                {
                    "kind": "frr",
                    "restore": [
                        "configure terminal",
                        "router bgp 65011",
                        "neighbor 10.0.0.1 route-map RM-ALLOW out",
                        "no route-map OWNED",
                        "no ip prefix-list OWNEDL",
                        "end",
                    ],
                }
            ],
        }
    )
    assert result["recovered"]
    assert "no route-map OWNED" not in calls[-1]
    assert "no ip prefix-list OWNEDL" not in calls[-1]
    assert "neighbor 10.0.0.1 route-map RM-ALLOW out" in calls[-1]


def test_validation_outage_keeps_comparison_evidence_without_claiming_unaffected_flow(monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import validate_effect
    from netopsbench.platform.toolkit._core.common import ToolResult

    manifest = build_clos_plan(config_for_scale("xs")).manifest
    toolkit = SimpleNamespace(get_device_status=lambda device: ToolResult(success=True, data={"running": False}))
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    pair = {"src": "client1", "dst": "client2"}
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="device_down",
        target_device="spine1",
        parameters={"affected_candidates": [pair], "control_candidates": [pair]},
    )
    observations = {
        "_probe_evidence": [{**pair, "baseline": {"samples": 4}, "fault": {"samples": 4}}],
        "pingmesh_metrics": {"anomalies": [{"src_name": "client1", "dst_name": "client2"}]},
    }
    result = validate_effect(
        SimpleNamespace(
            current_manifest=lambda: manifest,
            influxdb_bucket="worker",
            topology_id=manifest.topology_id,
            _case_affected_pairs=[pair],
        ),
        episode,
        {"observations": observations},
        {"nodes": {}},
        {"nodes": {}},
    )
    assert result["valid"]
    assert result["evidence"]["control_role"] == "comparison"
    assert result["evidence"]["probes"]
    assert "_probe_evidence" not in observations


def test_address_fault_rejects_network_overlap_before_writes(tmp_path, monkeypatch):
    from netopsbench.platform.topology.generator import generate_topology

    generate_topology("xs", str(tmp_path))
    injector = FaultInjector(clab_dir=str(tmp_path))
    handler = injector._network
    config_path = tmp_path / "configs" / "sonic" / "spine1" / "config_db.json"
    config = json.loads(config_path.read_text())
    config["INTERFACE"]["Ethernet0|198.51.100.5/24"] = {}
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(
        handler,
        "_json",
        lambda *args: [{"flags": ["UP"], "addr_info": [{"local": "10.1.1.1", "prefixlen": 30, "scope": "global"}]}],
    )
    calls = []
    monkeypatch.setattr(handler, "_run", lambda device, kind, args: calls.append(args) or "1")
    with pytest.raises(ValueError, match="non-overlapping"):
        handler._prepare(
            EpisodeSpec(
                episode_id="a",
                fault_type="interface_ip_misconfig",
                target_device="spine1",
                target_interface="Ethernet0",
            )
        )
    assert not any(args[0] == "config" for args in calls)


def test_address_replacement_and_restore_never_remove_the_last_address(tmp_path, monkeypatch):
    from netopsbench.platform.topology.generator import generate_topology

    generate_topology("xs", str(tmp_path))
    injector = FaultInjector(clab_dir=str(tmp_path))
    handler = injector._network
    original = "10.1.1.1/30"
    wrong = "198.51.100.1/30"
    monkeypatch.setattr(
        handler,
        "_json",
        lambda *args: [{"flags": ["UP"], "addr_info": [{"local": "10.1.1.1", "prefixlen": 30, "scope": "global"}]}],
    )
    monkeypatch.setattr(handler, "_run", lambda *args: "1")
    operations, _ = handler._prepare(
        EpisodeSpec(
            episode_id="a", fault_type="interface_ip_misconfig", target_device="spine1", target_interface="Ethernet0"
        )
    )
    addresses = {original}
    for commands in [*[op["apply"] for op in operations], *[op["restore"] for op in reversed(operations)]]:
        action, address = commands[3], commands[-1]
        if action == "add":
            addresses.add(address)
        else:
            assert len(addresses) > 1
            addresses.remove(address)
    assert addresses == {original}
    assert operations[0]["apply"][-1] == wrong


@pytest.mark.parametrize(
    "changes,peer,valid", [([-1, 1], "10.1.1.2", True), ([-1], "10.1.1.2", False), ([-1, 1], "10.9.9.2", False)]
)
def test_route_flapping_requires_actual_changes_at_the_selected_neighbor(injector, monkeypatch, changes, peer, valid):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import validate_effect
    from netopsbench.platform.toolkit._core.common import ToolResult

    manifest = injector._ctx.manifest
    calls = []

    def events(**kw):
        calls.append(kw)
        return ToolResult(
            success=True,
            data={
                "status": "ok",
                "events": [
                    {"device": "spine1", "peer": peer, "prefixes_before": 4, "prefixes_after": 4 + n} for n in changes
                ],
            },
        )

    toolkit = SimpleNamespace(
        get_device_status=lambda device: ToolResult(success=True, data={"running": True, "accessible": True}),
        get_device_interfaces=lambda *args, **kw: ToolResult(
            success=True, data={"addresses": {"Ethernet0": ["10.1.1.2/30"]}}
        ),
        get_bgp_neighbor=lambda *args: ToolResult(success=True, data={"state": "Established"}),
        query_bgp_events=events,
    )
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    controller = SimpleNamespace(
        error=None,
        history=[
            {"phase": phase, "time": f"2026-10-12T12:00:{i * 10:02d}Z"}
            for i, phase in enumerate(("fault", "healthy", "fault"))
        ],
    )
    injector._tracker._background_fault_controls["owned"] = {"controller": controller}
    episode = EpisodeSpec(
        episode_id="a",
        fault_type="route_flapping",
        target_device="leaf1",
        target_interface="Ethernet0",
        parameters={"peer_ip": "10.1.1.1"},
    )
    result = validate_effect(
        SimpleNamespace(
            current_manifest=lambda: manifest,
            injector=injector,
            _case_affected_pairs=[],
            influxdb_bucket="worker",
            topology_id=manifest.topology_id,
        ),
        episode,
        {
            "injection": {"control_id": "owned"},
            "observations": {"start_time": "2026-10-12T12:00:00Z", "end_time": "2026-10-12T12:00:30Z"},
        },
        {"nodes": {}},
        {"nodes": {}},
    )
    assert result["valid"] is valid
    assert calls[0]["device"] == "spine1" and calls[0]["peer"] == "10.1.1.2"


def test_loop_evidence_does_not_bind_native_pingmesh_udp_sockets(injector, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import validate_effect
    from netopsbench.platform.toolkit._core.common import ToolResult

    manifest = injector._ctx.manifest
    toolkit = SimpleNamespace(
        get_device_status=lambda device: ToolResult(success=True, data={"running": True, "accessible": True}),
        _resolve_container=lambda name: name,
    )
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    commands = []

    def trace(args, **kw):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="trace\n1 10.1.1.1\n2 10.1.1.2\n3 10.1.1.1\n4 10.1.1.2\n")

    monkeypatch.setattr("netopsbench.platform.scenario.effects.safe_run", trace)
    pair = {"src": "client1", "dst": "client3"}
    episode = EpisodeSpec(
        episode_id="a", fault_type="routing_loop", target_device="leaf1", parameters={"affected_candidates": [pair]}
    )
    result = validate_effect(
        SimpleNamespace(
            current_manifest=lambda: manifest,
            influxdb_bucket="worker",
            topology_id=manifest.topology_id,
            _case_affected_pairs=[pair],
        ),
        episode,
        {"observations": {"_probe_evidence": [{**pair, "baseline": {"samples": 4}}]}},
        {"nodes": {}},
        {"nodes": {}},
    )
    assert result["valid"]
    assert "-I" in commands[0]
    assert not any(arg.startswith("--sport") for arg in commands[0])


@pytest.mark.parametrize("output", ["", "% BGP instance not found\n", "Neighbor V AS State/PfxRcd\n"])
def test_phase_snapshot_distinguishes_unreadable_bgp_from_a_readable_empty_table(injector, monkeypatch, output):
    from netopsbench.platform.observability import routes

    payload = {"checks": {"fib": {"returncode": 0, "stdout": "[]"}, "bgp": {"returncode": 0, "stdout": output}}}
    monkeypatch.setattr(routes, "safe_run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(payload)))
    snapshot = routes.collect_routes(injector._ctx.manifest)
    expected = output.startswith("Neighbor")
    assert all(row["bgp_collection_ok"] is expected for row in snapshot["nodes"].values())
    assert all(row["bgp"] == [] for row in snapshot["nodes"].values())


@pytest.mark.parametrize("running", [False, None])
def test_device_fault_does_not_claim_an_already_stopped_or_unreadable_node(injector, monkeypatch, running):
    monkeypatch.setattr(injector._cmd, "container_is_running", lambda container: running)
    monkeypatch.setattr(injector._cmd, "run_cmd", lambda *args, **kw: pytest.fail("No mutation is authorized"))
    result = injector.inject_device_down("spine1")
    assert not result["success"]
    assert result["original_state"]["container_running"] is running
    assert not injector.active_faults


@pytest.mark.parametrize("method", ["inject_link_down", "inject_link_flapping"])
def test_link_fault_preserves_a_preexisting_admin_down_endpoint(injector, monkeypatch, method):
    monkeypatch.setattr(
        injector._cmd,
        "docker_exec",
        lambda *args, **kw: subprocess.CompletedProcess(args, 0, stdout="1: eth1: <BROADCAST,MULTICAST>"),
    )
    monkeypatch.setattr(
        injector._link, "_set_physical_link", lambda *args, **kw: pytest.fail("No link should be changed")
    )
    result = getattr(injector, method)("spine1", "Ethernet0")
    assert not result["success"]
    assert not injector.active_faults


@pytest.mark.parametrize("host_route", ["192.168.2.2", "192.168.2.2/32"])
def test_ttl_counters_need_both_real_exhaustion_and_returning_forwarding(injector, host_route):
    from types import SimpleNamespace

    from netopsbench.platform.scenario.effects import _ttl_loop_evidence

    episode = EpisodeSpec(
        episode_id="a",
        fault_type="routing_loop",
        target_device="spine1",
        target_interface="Ethernet0",
        target_prefix="192.168.2.2/32",
        parameters={"next_hop": "10.1.1.2"},
    )
    baseline = {
        "nodes": {
            "spine1": {
                "fib": [{"dst": "10.1.1.0/30", "dev": "Ethernet0", "protocol": "kernel", "prefsrc": "10.1.1.1"}],
                "packet_counters": {"icmp_out_time_exceeded": 0},
            },
            "leaf1": {"packet_counters": {"icmp_out_time_exceeded": 0}},
        }
    }
    current = {
        "nodes": {
            "spine1": {
                "fib": [{"dst": host_route, "gateway": "10.1.1.2"}],
                "packet_counters": {"icmp_out_time_exceeded": 0},
            },
            "leaf1": {
                "fib": [
                    {"dst": "default", "gateway": "172.31.140.1"},
                    {"dst": "192.168.2.0/30", "gateway": "10.1.1.1"},
                ],
                "packet_counters": {"icmp_out_time_exceeded": 12},
            },
        }
    }
    executor = SimpleNamespace(injector=injector)
    proof = _ttl_loop_evidence(executor, episode, baseline, current)
    assert proof["forwarding_cycle"] and proof["ttl_expirations"]["leaf1"]["after"] == 12
    current["nodes"]["leaf1"]["fib"][1]["gateway"] = "10.2.1.1"
    assert not _ttl_loop_evidence(executor, episode, baseline, current)["forwarding_cycle"]


def test_counter_collection_failure_never_becomes_zero(tmp_path):
    from netopsbench.platform.observability._routing_probe import packet_counters

    assert "icmp_out_time_exceeded" not in packet_counters(tmp_path / "missing")
    path = tmp_path / "snmp"
    path.write_text("Icmp: OutTimeExcds InTimeExcds\nIcmp: 7 3\n")
    assert packet_counters(path)["icmp_out_time_exceeded"] == 7
    path.write_text("Icmp: OutTimeExcds\nIcmp: missing\n")
    assert "error" in packet_counters(path)


@pytest.mark.parametrize("change", ["next_hop", "protocol"])
def test_recovery_rejects_a_surviving_route_error_with_the_same_path_count(change):
    from copy import deepcopy

    from netopsbench.platform.observability.routes import verify_route_recovery

    baseline = {
        "nodes": {
            "leaf1": {"fib": [{"dst": "192.168.2.0/30", "protocol": "bgp", "gateway": "10.1.1.1", "dev": "Ethernet0"}]}
        }
    }
    recovery = deepcopy(baseline)
    route = recovery["nodes"]["leaf1"]["fib"][0]
    route["gateway" if change == "next_hop" else "protocol"] = "10.2.1.1" if change == "next_hop" else "static"
    assert verify_route_recovery(baseline, recovery) == ["leaf1/192.168.2.0/30: forwarding not restored"]
    assert verify_route_recovery(baseline, baseline) == []


def test_effects_preflight_constructs_no_agent(monkeypatch, tmp_path):
    from scripts import validate_pingmesh_scales as script

    calls = []
    monkeypatch.setattr(script, "runtime_tools", lambda: calls.append("toolchain"))
    monkeypatch.setattr(script.subprocess, "check_output", lambda args, **kw: calls.append(args) or "sha256:image")
    monkeypatch.setattr(script, "agent_class", lambda _: pytest.fail("effects must not construct agents"))
    assert script.preflight_agents([], "deepseek", tmp_path) == {}
    assert len(calls) == 3


def test_failed_preparation_never_removes_another_owned_fault(injector, monkeypatch):
    injector._tracker.track({"type": "bgp_neighbor_shutdown", "device": "leaf1", "fault_id": "other-owner"})

    def fail(episode):
        raise RuntimeError("precondition failed before mutation")

    monkeypatch.setattr(injector._network, "_prepare", fail)
    result = injector._network.inject(
        EpisodeSpec(episode_id="a", fault_type="bgp_neighbor_shutdown", target_device="leaf1")
    )
    assert not result["success"]
    assert [fault.get("fault_id") for fault in injector.active_faults] == ["other-owner"]


def test_recovery_removes_an_owned_neighbor_option_with_its_value(injector, monkeypatch):
    handler = injector._network
    calls = []

    def run(device, kind, commands):
        calls.append(commands)
        return " neighbor 10.0.0.1 password opaque\n" if commands == ["show running-config"] else ""

    monkeypatch.setattr(handler, "_run", run)
    result = handler.recover(
        {
            "type": "bgp_auth_mismatch",
            "device": "leaf1",
            "journal": [
                {
                    "kind": "frr",
                    "restore": ["configure terminal", "router bgp 65011", "no neighbor 10.0.0.1 password", "end"],
                }
            ],
        }
    )
    assert result["recovered"]
    assert "no neighbor 10.0.0.1 password" in calls[-1]
