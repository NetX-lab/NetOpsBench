from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from netopsbench.platform.traffic import scenario_execution as scenario_execution_mod
from netopsbench.platform.traffic.controller import (
    TrafficController,
    TrafficFlow,
    _client_plans,
    _parse_bandwidth_bps,
    _plan_digest,
)


def _flows() -> list[TrafficFlow]:
    return [
        TrafficFlow(
            flow_id="flow-1",
            src="client1",
            dst="client2",
            dst_ip="192.168.102.2",
            dst_port=5201,
            protocol="udp",
            bandwidth="2M",
        ),
        TrafficFlow(
            flow_id="flow-2",
            src="client1",
            dst="client2",
            dst_ip="192.168.102.2",
            dst_port=5202,
            protocol="tcp",
            bandwidth="500K",
        ),
        TrafficFlow(
            flow_id="flow-3",
            src="client2",
            dst="client1",
            dst_ip="192.168.101.2",
            dst_port=5201,
            protocol="udp",
        ),
        TrafficFlow(
            flow_id="flow-4",
            src="client2",
            dst="client1",
            dst_ip="192.168.101.2",
            dst_port=5202,
            protocol="tcp",
        ),
    ]


def _controller() -> TrafficController:
    return TrafficController(
        {
            "client1": "172.20.20.101",
            "client2": "172.20.20.102",
        }
    )


class _NativeControl:
    def __init__(self):
        self.states: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, client: str, operation: str, payload: dict) -> dict:
        self.calls.append((client, operation, payload))
        state = self.states.setdefault(
            client,
            {
                "ready": False,
                "enabled": False,
                "generation": 0,
                "plan_digest": "",
                "expected_flows": 0,
                "active_flows": 0,
                "expected_listeners": 0,
                "active_listeners": 0,
            },
        )
        if operation == "load_plan":
            state.update(
                {
                    "ready": True,
                    "generation": payload["generation"],
                    "plan_digest": payload["plan_digest"],
                    "expected_flows": len(payload["plan"]["flows"]),
                    "active_flows": 0,
                    "expected_listeners": len(payload["plan"]["listeners"]),
                    "active_listeners": len(payload["plan"]["listeners"]),
                }
            )
        elif operation == "enable":
            state["enabled"] = True
            state["active_flows"] = state["expected_flows"]
            state["ready"] = True
        elif operation == "disable":
            state["enabled"] = False
            state["active_flows"] = 0
            state["ready"] = True
        state["heartbeat_unix_ns"] = time.time_ns()
        return {"protocol_version": 1, "ok": True, "status": dict(state)}


def test_compact_plan_preserves_existing_flow_contract():
    plans = _client_plans(_controller().management_ips, _flows())

    assert plans["client1"]["listeners"] == [
        {"protocol": "udp", "port": 5201},
        {"protocol": "tcp", "port": 5202},
    ]
    assert plans["client1"]["flows"] == [
        {
            "flow_id": "flow-1",
            "protocol": "udp",
            "dst_ip": "192.168.102.2",
            "dst_port": 5201,
            "bandwidth_bps": 2_000_000,
            "payload_bytes": 1400,
            "tcp_mss": 0,
        },
        {
            "flow_id": "flow-2",
            "protocol": "tcp",
            "dst_ip": "192.168.102.2",
            "dst_port": 5202,
            "bandwidth_bps": 500_000,
            "payload_bytes": 1360,
            "tcp_mss": 1360,
        },
    ]
    assert _plan_digest(plans["client1"]) == _plan_digest(plans["client1"])
    assert _parse_bandwidth_bps("1G") == 1_000_000_000


def test_synthetic_1024_client_plan_has_4096_flows():
    clients = {f"client{index}": f"172.31.{index // 254}.{index % 254 + 1}" for index in range(1024)}
    flows = [
        TrafficFlow(
            flow_id=f"flow-{source}-{offset}",
            src=f"client{source}",
            dst=f"client{(source + offset + 1) % 1024}",
            dst_ip=f"10.{((source + offset + 1) // 256) % 256}.{(source + offset + 1) % 256}.2",
            dst_port=5201 + offset,
            protocol="udp" if offset % 2 == 0 else "tcp",
        )
        for source in range(1024)
        for offset in range(4)
    ]

    plans = _client_plans(clients, flows)

    assert len(plans) == 1024
    assert sum(len(plan["flows"]) for plan in plans.values()) == 4096
    assert all(len(plan["flows"]) == 4 for plan in plans.values())
    assert all(len(plan["listeners"]) == 4 for plan in plans.values())


def test_start_matrix_loads_enables_and_checks_native_status(monkeypatch):
    native = _NativeControl()
    controller = _controller()
    monkeypatch.setattr(controller, "_request", native.request)

    flow_ids = controller.start_matrix(_flows())

    assert flow_ids == ["flow-1", "flow-2", "flow-3", "flow-4"]
    assert controller.verify_active_flows() is True
    assert len([call for call in native.calls if call[1] == "load_plan"]) == 2
    assert len([call for call in native.calls if call[1] == "enable"]) == 2
    assert all(call[0].startswith("client") for call in native.calls)


def test_stale_heartbeat_or_partial_flow_is_not_ready(monkeypatch):
    native = _NativeControl()
    controller = _controller()
    monkeypatch.setattr(controller, "_request", native.request)
    controller.start_matrix(_flows())

    native.states["client1"]["active_flows"] -= 1
    assert controller.verify_active_flows() is False

    native.states["client1"]["active_flows"] = native.states["client1"]["expected_flows"]

    def stale_request(client: str, operation: str, payload: dict) -> dict:
        response = native.request(client, operation, payload)
        response["status"]["heartbeat_unix_ns"] = time.time_ns() - 20_000_000_000
        return response

    monkeypatch.setattr(controller, "_request", stale_request)
    assert controller.verify_active_flows() is False


def test_generation_or_digest_mismatch_is_not_ready(monkeypatch):
    native = _NativeControl()
    controller = _controller()
    monkeypatch.setattr(controller, "_request", native.request)
    controller.start_matrix(_flows())

    native.states["client2"]["plan_digest"] = "wrong"
    assert controller.verify_active_flows() is False


def test_transient_management_timeout_is_retried_once(monkeypatch):
    native = _NativeControl()
    controller = _controller()
    attempts = 0

    def transient_request(client: str, operation: str, payload: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("injected timeout")
        return native.request(client, operation, payload)

    monkeypatch.setattr(controller, "_request", transient_request)

    response = controller._request_with_retry("client1", "status", {})

    assert response["ok"] is True
    assert attempts == 2


def test_failed_start_disables_loaded_agents_and_keeps_no_active_flows(monkeypatch):
    native = _NativeControl()
    controller = _controller()

    def fail_enable(client: str, operation: str, payload: dict) -> dict:
        if client == "client2" and operation == "enable":
            raise RuntimeError("injected enable failure")
        return native.request(client, operation, payload)

    monkeypatch.setattr(controller, "_request", fail_enable)
    with pytest.raises(RuntimeError, match="injected enable failure"):
        controller.start_matrix(_flows())

    assert controller.active_flows == {}
    assert controller.generation == 0
    assert any(operation == "disable" for _, operation, _ in native.calls)


def test_stop_all_disables_every_agent(monkeypatch):
    native = _NativeControl()
    controller = _controller()
    monkeypatch.setattr(controller, "_request", native.request)
    controller.start_matrix(_flows())

    controller.stop_all()

    assert controller.active_flows == {}
    assert controller.generation == 0
    assert len([call for call in native.calls if call[1] == "disable"]) == 2


def test_setup_traffic_cleans_partial_matrix_and_fails_before_baseline(tmp_path, monkeypatch):
    (tmp_path / "topology.json").write_text("{}", encoding="utf-8")
    traffic_config = {
        "stats": {
            "total_flows": 2,
            "udp_flows": 1,
            "tcp_flows": 1,
            "estimated_switch_pps": {},
        },
        "profile": {},
        "flows": [
            {
                "src": "client1",
                "dst": "client2",
                "dst_ip": "192.0.2.2",
                "protocol": "udp",
            },
            {
                "src": "client2",
                "dst": "client1",
                "dst_ip": "192.0.2.1",
                "protocol": "tcp",
            },
        ],
    }
    controller_instances = []

    class PartialController:
        def __init__(self, _management_ips):
            self.last_start_stats = SimpleNamespace(to_dict=lambda: {"started_flow_count": 1})
            self.stop_calls = 0
            controller_instances.append(self)

        def start_matrix(self, flows):
            return [flows[0].flow_id]

        def stop_all(self):
            self.stop_calls += 1

    clients = [
        SimpleNamespace(name="client1", mgmt_ip="172.20.0.1"),
        SimpleNamespace(name="client2", mgmt_ip="172.20.0.2"),
    ]
    monkeypatch.setattr(
        scenario_execution_mod,
        "generate_traffic_config",
        lambda *args, **kwargs: traffic_config,
    )
    monkeypatch.setattr(
        scenario_execution_mod,
        "validate_traffic_config",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        scenario_execution_mod,
        "load_topology_manifest",
        lambda _path: SimpleNamespace(clients=lambda: clients),
    )
    monkeypatch.setattr(
        scenario_execution_mod,
        "TrafficController",
        PartialController,
    )
    runner = SimpleNamespace(
        topology_dir=tmp_path,
        scale_registry=SimpleNamespace(),
        traffic_controller=None,
    )

    with pytest.raises(RuntimeError, match="started 1/2 flows"):
        scenario_execution_mod.setup_traffic(runner, "xs", "standard")

    assert controller_instances[0].stop_calls == 1
    assert runner.traffic_controller is None
