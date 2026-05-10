"""Regression tests for structured active fault tracking."""

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.models import ActiveFault


def test_active_fault_dataclass_roundtrips_metadata_to_dict():
    fault = ActiveFault(
        type="packet_loss",
        device="leaf1",
        interface="eth1",
        metadata={"loss_pct": 10, "linux_interface": "eth1"},
    )

    payload = fault.to_dict()

    assert payload["type"] == "packet_loss"
    assert payload["device"] == "leaf1"
    assert payload["interface"] == "eth1"
    assert payload["loss_pct"] == 10
    assert payload["linux_interface"] == "eth1"


def test_get_active_faults_preserves_public_dict_shape():
    injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    injector.active_faults = [
        ActiveFault(
            type="static_route_misconfig",
            device="leaf1",
            metadata={"target_ip": "192.168.1.2/32", "wrong_nexthop": "192.168.1.1"},
        )
    ]

    active = injector.get_active_faults()

    assert active == [
        {
            "type": "static_route_misconfig",
            "device": "leaf1",
            "target_ip": "192.168.1.2/32",
            "wrong_nexthop": "192.168.1.1",
            "success": True,
            "error": None,
        }
    ]


def test_link_flapping_uses_python_background_control_metadata():
    injector = FaultInjector(
        topology_metadata={"name": "dcn", "devices": {"spines": [{"name": "spine1"}], "leafs": [], "clients": []}}
    )
    injector.container_names = {"spine1": "clab-dcn-spine1"}

    stub = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    injector._sonic.config_cmd = lambda *args, **kwargs: stub
    injector._cmd.docker_exec = lambda *args, **kwargs: stub

    fault = injector.inject_link_flapping(device="spine1", interface="Ethernet0", iterations=1, down_time=0, up_time=0)

    assert fault["type"] == "link_flapping"
    assert fault["orchestration"] == "python"
    assert "task_id" in fault
    assert "pid" not in fault


def test_fault_injector_normalizes_handler_result_to_legacy_dict():
    normalized = FaultInjector._legacy_fault_result(
        {"recovered": True, "type": "link_down", "device": "leaf1", "error": None}
    )

    assert normalized == {
        "success": True,
        "error": None,
        "recovered": True,
        "type": "link_down",
        "device": "leaf1",
    }
