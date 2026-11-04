from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.platform.scenario.baseline import BaselineHandoff, runtime_scope, valid_window
from netopsbench.platform.scenario.executor import ScenarioExecutor
from netopsbench.platform.topology.generator import generate_topology


def window(age=0, duration=66):
    end = datetime.now(UTC) - timedelta(seconds=age)
    return {
        "name": "baseline",
        "start_time": (end - timedelta(seconds=duration)).isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": duration,
    }


def observation(*, healthy=True, complete=True):
    return {
        **window(),
        "data_source_status": "ok",
        "coverage_status": "complete" if complete else "incomplete",
        "_baseline_coverage": {"coverage_status": "complete"},
        "_baseline_health": {
            "absolute_unreachable_paths": 0 if healthy else 2,
            "absolute_packet_loss_paths": 0,
            "absolute_network_mtu_paths": 0,
        },
        "pingmesh_metrics": {"summary": {"latency_spikes": 0}, "quality": {"current_paths_observed": 2}},
    }


def scenario(name="case", fault="none"):
    return ScenarioSpec(
        scenario_id=name,
        name=name,
        topology_scale="xs",
        episode=EpisodeSpec(
            episode_id="diagnosis",
            fault_type=fault,
            duration_seconds=66,
            target_device="leaf1" if fault != "none" else None,
        ),
    )


@pytest.mark.parametrize("age,duration,expected", [(0, 66, True), (121, 66, False), (-10, 66, False), (0, 30, False)])
def test_window_age_and_full_coverage_duration(age, duration, expected):
    assert valid_window(window(age, duration), 60) is expected


def test_handoff_is_single_use_and_epoch_does_not_roll_forward(monkeypatch):
    import netopsbench.platform.scenario.baseline as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    h = BaselineHandoff()
    h.fresh_epoch()
    assert h.publish(window(), "scope", "case1", 60)
    assert h.take("scope", 60)[0] is not None
    assert h.take("scope", 60)[0] is None
    clock[0] = 590
    assert h.publish(window(), "scope", "case2", 60)
    clock[0] = 601
    assert h.take("scope", 60) == (None, "recalibration_due")


def test_changed_scope_rejects_handoff():
    h = BaselineHandoff()
    h.fresh_epoch()
    h.publish(window(), "old", "case", 60)
    assert h.take("changed", 60)[0] is None


def test_only_a_new_verified_window_refreshes_calibration(monkeypatch):
    import netopsbench.platform.scenario.baseline as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    handoff = BaselineHandoff()
    first = window()
    handoff.fresh_epoch(first)
    assert handoff.publish(first, "scope", "first", 60, verified=True)
    assert handoff.take("scope", 60)[0] == first

    clock[0] = 601
    newer = window()
    assert handoff.publish(newer, "scope", "recovered", 60, verified=True)
    assert handoff.epoch == 601
    assert handoff.take("scope", 60)[0] == newer

    clock[0] = 1202
    assert handoff.publish(newer, "scope", "repeated", 60, verified=True)
    assert handoff.epoch == 601
    assert handoff.take("scope", 60) == (None, "recalibration_due")


@pytest.mark.parametrize(
    "age,duration,reason", [(121, 66, "expired_window"), (-10, 66, "future_window"), (0, 30, "short_window")]
)
def test_rejected_publication_preserves_the_reason(age, duration, reason):
    handoff = BaselineHandoff()
    handoff.fresh_epoch()
    assert not handoff.publish(window(age, duration), "scope", "case", 60, verified=True)
    assert handoff.last_reason == reason
    assert handoff.take("scope", 60) == (None, reason)


def test_unavailable_and_changed_scopes_have_distinct_reasons():
    handoff = BaselineHandoff()
    assert not handoff.publish(window(), None, "case", 60, verified=True)
    assert handoff.last_reason == "scope_unavailable"
    handoff.publish(window(), "old", "case", 60, verified=True)
    assert handoff.take("new", 60) == (None, "scope_changed")


def test_scope_differences_identify_the_changed_component():
    from netopsbench.platform.scenario.baseline import RuntimeScope, scope_changes

    first = RuntimeScope({"plan": "p"}, {"frr/a": "old"}, {"a": ["id"]})
    second = RuntimeScope({"plan": "p"}, {"frr/a": "new"}, {"a": ["id"]})
    assert scope_changes(first, second) == ["configs:frr/a"]
    assert first.digest != second.digest


def test_frr_normalization_preserves_context_and_order():
    from netopsbench.platform.scenario.baseline import normalized_frr

    text = "frr version 10.3\nrouter bgp 65001\n neighbor 10.0.0.1 remote-as 65002\n!\nline vty\n"
    assert normalized_frr(text) == normalized_frr(
        "Building configuration...\nCurrent configuration:\n! saved now\n" + text + "end\n"
    )
    assert normalized_frr(text) != normalized_frr(text.replace(" neighbor", "neighbor"))
    assert normalized_frr(text) != normalized_frr(text.replace("65002", "65003"))


def test_only_proven_owned_rewrite_can_change_recovery_scope():
    from netopsbench.platform.scenario.baseline import RuntimeScope, restored_scope_errors

    before = RuntimeScope({"plan": "p"}, {"configs/frr/leaf1.conf": "old"}, {})
    after = RuntimeScope({"plan": "p"}, {"configs/frr/leaf1.conf": "new"}, {})
    case = scenario(fault="link_down")
    assert restored_scope_errors(before, after, case, None)
    assert not restored_scope_errors(before, after, case, {"restored": True})
    after.identity["plan"] = "changed"
    assert restored_scope_errors(before, after, case, {"restored": True}) == ["identity:plan"]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    metadata = generate_topology("xs", str(tmp_path), name="test")["metadata"]
    r = ScenarioExecutor(topology_metadata=metadata, topology_dir=str(tmp_path), sleep_fn=lambda _: None)
    r.calls = []

    def setup(*args):
        r.calls.append("traffic")
        r.traffic_controller = SimpleNamespace(active_flows={"a": 1}, verify_active_flows=lambda: True)
        return {}

    def reference():
        r.calls.append("reference")
        return window()

    def observe(*a, **k):
        r.calls.append("observe")
        return observation()

    monkeypatch.setattr(r, "_setup_traffic", setup)
    monkeypatch.setattr(r, "_capture_baseline_window", reference)
    monkeypatch.setattr(r, "_capture_observation_window", lambda seconds, name: window(duration=seconds))
    monkeypatch.setattr(r, "_wait_and_observe", observe)
    monkeypatch.setattr(r, "_merge_observation_windows", observe)
    monkeypatch.setattr(r, "_baseline_scope", lambda _: "scope")
    monkeypatch.setattr(r, "_cleanup_after_scenario", lambda *a, **k: {"success": True, "status": "clean"})
    return r


def run(r, name, fault="none"):
    return r.run_scenario(scenario(name, fault), diagnosis_callback=lambda *a, **k: {"verdict": "network_healthy"})


def test_two_healthy_cases_keep_independent_observations_and_share_verified_baseline(runner):
    first = run(runner, "one")
    second = run(runner, "two")
    assert first["success"] and second["success"]
    assert first["baseline_preparation"]["mode"] == "fresh"
    assert second["baseline_preparation"] == {
        "mode": "reused",
        "reason": "verified_previous_case",
        "source_scenario_id": "one",
        "scope_changes": [],
    }
    assert runner.calls == ["traffic", "reference", "observe", "observe", "observe"]
    assert first["cleanup"]["baseline_handoff"] and second["cleanup"]["baseline_handoff"]


def test_failed_baseline_retains_window_coverage_and_health_without_diagnosis(runner, monkeypatch):
    validation = observation()
    validation["_baseline_health"]["absolute_network_mtu_paths"] = 1
    validation["_coverage_audit"] = {"plan_id": "current-plan", "coverage_status": "complete"}
    monkeypatch.setattr(runner, "_merge_observation_windows", lambda *a, **k: validation)

    result = run(runner, "failed-baseline")

    assert result["case_valid"] is False
    assert result["episode"] is None
    assert "absolute_network_mtu_paths=1" in result["error"]
    assert result["baseline_preparation"]["failed_validation"] == validation
    assert result["cleanup"]["success"] is True


@pytest.mark.parametrize("invalid", ["scope", "expiry", "cleanup", "unhealthy", "incomplete", "missing_absolute"])
def test_invalid_candidate_forces_fresh_single_baseline(runner, monkeypatch, invalid):
    if invalid in {"unhealthy", "incomplete", "missing_absolute"}:
        runner._healthy_baseline_candidate = None
        candidate = observation(healthy=invalid != "unhealthy", complete=invalid != "incomplete")
        if invalid == "missing_absolute":
            candidate.pop("_baseline_health")
        runner._offer_healthy_window(candidate)
        assert runner._healthy_baseline_candidate is None
        return
    if invalid == "cleanup":
        monkeypatch.setattr(runner, "_cleanup_after_scenario", lambda *a, **k: {"success": False})
    run(runner, "one")
    if invalid == "scope":
        monkeypatch.setattr(runner, "_baseline_scope", lambda _: "new")
    if invalid == "expiry":
        runner._baseline_handoff.window = window(age=121)
    monkeypatch.setattr(runner, "_cleanup_after_scenario", lambda *a, **k: {"success": True})
    second = run(runner, "two")
    assert second["baseline_preparation"]["mode"] == "fresh"
    assert runner.calls.count("reference") == 2


@pytest.mark.parametrize("next_fault", ["none", "link_down"])
def test_fault_recovery_window_is_handed_to_next_case(runner, monkeypatch, next_fault):
    monkeypatch.setattr(runner, "_inject_fault", lambda *a: {"success": True})
    monkeypatch.setattr(runner, "_capture_observation_window", lambda seconds, name: window(duration=seconds))
    monkeypatch.setattr(
        runner,
        "_merge_observation_windows",
        lambda windows, *a, **k: observation(healthy=all(w.get("name") == "baseline" for w in windows)),
    )
    monkeypatch.setattr(
        runner, "_cleanup_after_scenario", lambda *a, **k: {"success": True, "validated_baseline": window()}
    )
    first = run(runner, "fault", "link_down")
    second = run(runner, "next", next_fault)
    assert first["case_valid"] and first["cleanup"]["baseline_handoff"]
    assert second["baseline_preparation"]["mode"] == "reused"


def test_verified_recovery_window_is_bound_to_its_new_identity(runner, monkeypatch):
    identities = iter(["before", "recovered", "recovered", "recovered"])
    monkeypatch.setattr(runner, "_baseline_scope", lambda _: next(identities))
    monkeypatch.setattr(
        runner,
        "_cleanup_after_scenario",
        lambda *a, **k: {"success": True, "validated_baseline": window(), "validated_baseline_scope": "recovered"},
    )
    first = run(runner, "one")
    second = run(runner, "two")
    assert first["cleanup"]["baseline_handoff"]
    assert second["baseline_preparation"]["mode"] == "reused"
    assert second["baseline_preparation"]["scope_changes"] == []


def test_invalid_case_can_publish_an_independently_verified_recovery(runner, monkeypatch):
    def analyze(windows, *args, **kwargs):
        if any(w.get("name") != "baseline" for w in windows):
            raise RuntimeError("Fault observation is invalid")
        return observation()

    monkeypatch.setattr(runner, "_inject_fault", lambda *a: {"success": True})
    monkeypatch.setattr(runner, "_merge_observation_windows", analyze)
    monkeypatch.setattr(
        runner,
        "_cleanup_after_scenario",
        lambda *a, **k: {"success": True, "validated_baseline": window(), "validated_baseline_scope": "scope"},
    )
    first = run(runner, "invalid", "link_down")
    assert first["case_valid"] is False
    assert first["success"] is False
    assert first["cleanup"]["baseline_handoff"] is True
    assert first["cleanup"]["baseline_handoff_reason"] == "verified_window_published"
    monkeypatch.setattr(runner, "_merge_observation_windows", lambda *a, **k: observation())
    second = run(runner, "next")
    assert second["baseline_preparation"]["mode"] == "reused"
    assert second["baseline_preparation"]["source_scenario_id"] == "invalid"


def test_agent_failure_keeps_case_failure_and_verified_recovery_handoff(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_cleanup_after_scenario",
        lambda *a, **k: {"success": True, "validated_baseline": window(), "validated_baseline_scope": "scope"},
    )
    first = runner.run_scenario(
        scenario("failed-agent"), diagnosis_callback=lambda *a, **k: {"success": False, "error": "agent failed"}
    )
    assert first["case_valid"] is True
    assert first["success"] is False
    assert first["cleanup"]["baseline_handoff"] is True
    assert run(runner, "next")["baseline_preparation"]["mode"] == "reused"


def test_runtime_scope_changes_on_restart_config_policy_and_traffic(tmp_path, monkeypatch):
    from netopsbench.platform.scenario import baseline

    metadata = generate_topology("xs", str(tmp_path), name="test")["metadata"]
    r = ScenarioExecutor(topology_metadata=metadata, topology_dir=str(tmp_path))
    r.runtime_worker = SimpleNamespace(lab_name="test", runtime_id="test", worker_id="worker-1")
    r.traffic_controller = SimpleNamespace(generation=1, plan_digests={"client1": "plan"})
    started = ["first"]
    monkeypatch.setattr(
        baseline,
        "safe_run",
        lambda *a, **k: SimpleNamespace(stdout="\n".join(f"id{i} image {started[0]} 1 true false" for i in range(6))),
    )
    a = runtime_scope(r, scenario())
    assert a
    r.traffic_controller.plan_digests["client1"] = "other"
    assert runtime_scope(r, scenario()) != a
    r.traffic_controller.plan_digests["client1"] = "plan"
    assert runtime_scope(r, scenario()) == a
    r.traffic_controller.generation = 2
    assert runtime_scope(r, scenario()) != a
    r.traffic_controller.generation = 1
    started[0] = "restarted"
    assert runtime_scope(r, scenario()) != a
    started[0] = "first"
    (tmp_path / "configs" / "changed").write_text("changed")
    assert runtime_scope(r, scenario()) != a
    (tmp_path / "configs" / "changed").unlink()
    assert runtime_scope(r, scenario()) == a
    r.topology_id = "different"
    assert runtime_scope(r, scenario()) != a


def test_observation_reuses_static_detector_and_queries_each_window(tmp_path, monkeypatch):
    from netopsbench.platform.pingmesh import detector
    from netopsbench.platform.scenario.observation import analyze_observation_windows
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology("xs", str(tmp_path), name="reuse-xs")["metadata"]
    runner = ScenarioExecutor(topology_dir=str(tmp_path), topology_metadata=metadata)
    instances, queries = [], []

    class Detector:
        def __init__(self, **kwargs):
            instances.append(kwargs)

        def generate_windowed_anomaly_report(self, **kwargs):
            queries.append(kwargs)
            return {"query_status": {"ok": True}, "summary": {}, "coverage": {"coverage_status": "complete"}}

    monkeypatch.setattr(detector, "AnomalyDetector", Detector)
    baseline = {"start_time": "2026-10-12T00:00:00Z", "end_time": "2026-10-12T00:01:00Z"}
    window = {"name": "fault", "start_time": "2026-10-12T00:02:00Z", "end_time": "2026-10-12T00:03:00Z"}
    for _ in range(2):
        analyze_observation_windows(runner, [window], 60, baseline)
    assert len(instances) == 1 and len(queries) == 2
    assert instances[0]["topology_metadata"] is runner.manifest
    runner.influxdb_bucket = "new-bucket"
    analyze_observation_windows(runner, [window], 60, baseline)
    assert len(instances) == 2 and len(queries) == 3


@pytest.mark.parametrize("entry", ["benchmark", "simulator"])
def test_entrypoints_share_single_capture_and_verified_reuse(runner, monkeypatch, entry):
    from netopsbench.models.profiles import default_scale_registry
    from netopsbench.platform.incident.engine import IncidentEngine
    from netopsbench.platform.simulator.runtime import RuntimeEpisodeBackend, WarmRuntime

    worker = SimpleNamespace(topology_dir=runner.topology_dir, bucket="bucket", topology_id=runner.topology_id)
    record = WarmRuntime(runtime=SimpleNamespace(workers=[worker]), runner=runner)
    leases = SimpleNamespace(acquire=lambda scale: record, release=lambda record: None)
    monkeypatch.setattr("netopsbench.platform.simulator.runtime.require_scenario_topology", lambda *args: None)
    for name in ("one", "two"):
        if entry == "benchmark":
            assert run(runner, name)["success"]
        else:
            backend = RuntimeEpisodeBackend(leases, default_scale_registry())
            incident = IncidentEngine(lambda backend=backend: backend).prepare(scenario(name))
            assert incident.state.value == "active", incident.failure
            incident.close()
            assert incident.cleanup_status.value == "succeeded", incident.cleanup_failure
    assert runner.calls == ["traffic", "reference", "observe", "observe", "observe"]


def test_cleanup_exception_before_result_retains_original_failure(runner, monkeypatch):
    from netopsbench.platform.scenario.incident_backend import ExecutorIncidentBackend

    backend = ExecutorIncidentBackend(runner)
    backend.scenario = scenario()
    monkeypatch.setattr(
        runner, "_cleanup_after_scenario", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("restore failed"))
    )
    with pytest.raises(RuntimeError, match="restore failed"):
        backend.finish()
    assert backend.cleanup_result["success"] is False
    assert backend.cleanup_result["errors"] == ["restore failed"]
    assert runner._recovering is False
    backend.finish()  # idempotent; original failed outcome is retained
