"""Recovery cannot trade missing state evidence for a healthy ping result."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from netopsbench.models.recovery import RecoveryPolicy
from netopsbench.platform.scenario.baseline import BaselineHandoff, RuntimeScope


def window(age=0):
    end = datetime.now(UTC) - timedelta(seconds=age)
    return {
        "name": "baseline",
        "start_time": (end - timedelta(seconds=30)).isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": 30,
    }


def test_policy_does_not_rewrite_legacy_metadata():
    metadata = {"experiment": {"observation_seconds": 30}}
    with pytest.raises(ValueError, match="migrate"):
        RecoveryPolicy.from_metadata(metadata)
    assert "recovery_policy" not in metadata
    assert RecoveryPolicy().version == 2
    assert RecoveryPolicy().baseline_max_cases == 10
    with pytest.raises(ValueError):
        RecoveryPolicy(version=1)


def test_baseline_ten_uses_and_interrupted_consumer():
    h = BaselineHandoff(max_cases=10, max_age_seconds=600)
    scope = RuntimeScope({}, {}, {})
    w = window()
    assert h.publish(w, scope, "initial", 30, verified=True)
    for i in range(10):
        assert h.take(scope, 30)[0] == w
        assert h.uses == i + 1
        assert h.take(scope, 30) == (None, "previous_case_not_verified")
        h.ready = True  # Only a successful recovery grants this permission.
    assert h.take(scope, 30) == (None, "case_limit")
    assert h.window == w


@pytest.mark.parametrize("age,allowed", [(599, True), (601, False)])
def test_reusable_age_is_original_window_age(age, allowed):
    h = BaselineHandoff(max_age_seconds=600)
    scope = RuntimeScope({}, {}, {})
    assert h.publish(window(age), scope, "initial", 30, verified=True) is allowed
    assert (h.take(scope, 30)[0] is not None) is allowed


def test_identity_change_and_failure_invalidate_reuse():
    h = BaselineHandoff(max_age_seconds=600)
    h.publish(window(), RuntimeScope({"generation": 1}, {}, {}), "initial", 30, verified=True)
    assert h.take(RuntimeScope({"generation": 2}, {}, {}), 30)[1] == "scope_changed"
    h.clear("recovery_failed")
    assert h.take(None, 30) == (None, "recovery_failed")


def test_exact_age_boundary(monkeypatch):
    from netopsbench.platform.scenario import baseline

    now = datetime.now(UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(baseline, "datetime", FrozenDatetime)
    end = now - timedelta(seconds=600)
    w = {
        "start_time": (end - timedelta(seconds=30)).isoformat(),
        "end_time": end.isoformat(),
        "duration_seconds": 30,
    }
    assert baseline.window_error(w, 30, max_age_seconds=600) is None
    now += timedelta(microseconds=1)
    assert baseline.window_error(w, 30, max_age_seconds=600) == "expired_window"


@pytest.mark.parametrize("wrong_neighbor,flap", [(False, False), (True, False), (False, True)])
def test_state_health_uses_identities_and_final_full_pass(monkeypatch, wrong_neighbor, flap):
    import time

    from netopsbench.platform.runtime import health
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import config_for_scale
    from netopsbench.platform.utils.control import OperationControl
    from netopsbench.platform.utils.interface_names import to_sonic_interface

    topo = build_clos_plan(config_for_scale("xs")).manifest
    worker = SimpleNamespace(lab_name=topo.name, topology_id=topo.topology_id, topology_dir="unused", bucket="test")
    counts = health.expected_network_counts(topo)
    calls = {}
    expected = {"nodes": {}}
    for device in topo.routing_devices():
        expected["nodes"][device.name] = {
            "fib": [],
            "bgp_collection_ok": True,
            "bgp": [
                {"neighbor": f"10.1.1.{i + 1}", "asn": 65001, "state": "Established"}
                for i in range(counts[device.name]["bgp_neighbors"])
            ],
        }

    def routing(container, control=None, route_snapshot=None):
        device = container.removeprefix(f"clab-{topo.name}-")
        calls[device] = calls.get(device, 0) + 1
        route_snapshot.update(fib=[], bgp_collection_ok=True)
        prefix = "10.9.9" if wrong_neighbor or (flap and calls[device] == 2) else "10.1.1"
        bgp = "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd\n" + "\n".join(
            f"{prefix}.{i + 1} 4 65001 1 1 0 0 0 00:10 2" for i in range(counts[device]["bgp_neighbors"])
        )
        interfaces = "\n".join(
            to_sonic_interface(e.interface) for link in topo.links for e in link.endpoints if e.device == device
        )
        return bgp, interfaces

    def run(args, **kwargs):
        if "inspect" in args:
            names = [f"clab-{topo.name}-{d.name}" for d in topo.devices]
            return SimpleNamespace(returncode=0, stdout="\n".join(f"/{n} true false" for n in names))
        return SimpleNamespace(returncode=0, stdout=f"telegraf-{topo.name}")

    monkeypatch.setattr(health, "_routing_state", routing)
    monkeypatch.setattr(health, "_parse_active_interfaces", lambda output: set(output.splitlines()))
    monkeypatch.setattr(health, "_running_container_count", lambda *a, **k: len(topo.devices))
    monkeypatch.setattr(health, "safe_run", run)
    commands = []
    monkeypatch.setattr(health, "_docker_exec", lambda *a, **k: commands.append(a) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        health,
        "request_agent",
        lambda *a, **k: {
            "ok": True,
            "status": {
                "ready": True,
                "heartbeat_unix_ns": time.time_ns(),
                "plan_id": topo.require_pingmesh_plan().settings.plan_id,
                "config_schema_version": 2,
            },
        },
    )
    audit = {"network_rounds": 3}  # Preserve rounds from an earlier bounded recovery attempt.
    errors = health._check_worker_health(
        worker,
        health_retries=2,
        health_delay=0,
        all_routing_devices=True,
        topology=topo,
        control=OperationControl(deadline=time.monotonic() + 10),
        route_snapshot={"nodes": {}},
        expected_routes=expected,
        state_only=True,
        recovery_audit=audit,
    )
    assert bool(errors) == (wrong_neighbor or flap)
    assert set(calls.values()) == {2}
    assert not any("logger" in str(command) for command in commands)
    if not errors:
        assert audit["network_rounds"] == 5 and audit["collected_at"] > 0


def test_acl_recovery_exit_zero_does_not_hide_residual_rules(monkeypatch):
    from netopsbench.platform.faults.handlers.network import NetworkFaultHandler

    handler = NetworkFaultHandler(
        None, None, None, None, SimpleNamespace(remove_faults=lambda f: pytest.fail("removed")), None
    )
    fault = {
        "type": "acl_l4_port_misconfig",
        "device": "leaf1",
        "fault_id": "owned",
        "journal": [{"kind": "docker", "restore": ["true"]}],
        "evidence": {"original_iptables": "-P FORWARD ACCEPT", "acl_name": "owned"},
    }
    monkeypatch.setattr(handler, "_run", lambda *args: "-P FORWARD ACCEPT\n-A FORWARD -j DROP")
    result = handler.recover(fault)
    assert not result["recovered"] and not result["readback_verified"]
    assert "Actual ACL rules" in result["error"]


def test_frr_recovery_exit_zero_does_not_hide_config_drift(monkeypatch):
    from netopsbench.platform.faults.handlers.network import NetworkFaultHandler
    from netopsbench.platform.scenario.baseline import normalized_frr

    handler = NetworkFaultHandler(
        None, None, None, None, SimpleNamespace(remove_faults=lambda f: pytest.fail("removed")), None
    )
    fault = {
        "type": "bgp_auth_mismatch",
        "device": "leaf1",
        "fault_id": "owned",
        "journal": [{"kind": "docker", "restore": ["true"]}],
        "evidence": {"original_frr_digest": normalized_frr("router bgp 65001\n")},
    }
    monkeypatch.setattr(handler, "_run", lambda *args: "router bgp 65001\n neighbor 10.1.1.1 password wrong\n")
    assert handler.recover(fault)["recovered"] is False


def test_terminal_destroy_skips_window_but_never_accepts_active_faults():
    from netopsbench.platform.scenario.executor import ScenarioExecutor

    runner = object.__new__(ScenarioExecutor)
    runner.injector = SimpleNamespace(active_faults=[])
    runner.calibrate = lambda *a, **k: pytest.fail("destroy path must not collect a window")
    assert runner.final_calibration(None, destroy_after_run=True) == {
        "status": "skipped",
        "reason": "runtime_will_be_destroyed",
        "validation_level": "state_checked",
    }
    runner.injector.active_faults.append({"residual": True})
    with pytest.raises(RuntimeError, match="unresolved"):
        runner.final_calibration(None, destroy_after_run=True)
