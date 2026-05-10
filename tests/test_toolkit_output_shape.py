from __future__ import annotations

import subprocess

from netopsbench.platform.toolkit.toolkit import AgentToolkit


class _Response:
    status_code = 200
    text = ""

    def __init__(self, text: str):
        self.text = text


def _toolkit() -> AgentToolkit:
    return AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.15"}],
                "clients": [],
            },
        }
    )


def test_get_device_logs_excludes_raw_csv_by_default(monkeypatch):
    toolkit = _toolkit()
    csv_text = "_time,source,severity,_value\n2026-05-04T00:00:00Z,leaf1,notice,message-one\n"

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.log_ops.requests.post",
        lambda *args, **kwargs: _Response(csv_text),
    )

    result = toolkit.get_device_logs("leaf1", time_range_minutes=10)

    assert result.success is True
    assert "logs" in result.data
    assert "raw_csv" not in result.data


def test_get_device_logs_can_include_raw_csv(monkeypatch):
    toolkit = _toolkit()
    csv_text = "_time,source,severity,_value\n2026-05-04T00:00:00Z,leaf1,notice,message-one\n"

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.log_ops.requests.post",
        lambda *args, **kwargs: _Response(csv_text),
    )

    result = toolkit.get_device_logs("leaf1", time_range_minutes=10, include_raw=True)

    assert result.success is True
    assert result.data["raw_csv"] == csv_text


def test_get_device_interfaces_summary_keeps_diagnostic_fields(monkeypatch):
    toolkit = _toolkit()

    def fake_exec(container, cmd_args, timeout):
        command = " ".join(cmd_args)
        if "status" in command:
            stdout = "Interface    Admin    Oper    Speed\nEthernet0    up       up      100G\nEthernet4    up       down    100G\n"
        else:
            stdout = "IFACE      RX_ERR    TX_ERR    RX_DRP    TX_DRP\nEthernet0  0         0         0         0\nEthernet4  5         0         2         0\n"
        return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(toolkit, "_docker_exec", fake_exec)

    result = toolkit.get_device_interfaces("leaf1", format="summary")

    assert result.success is True
    assert result.data["view"] == "summary"
    assert result.data["interface_count"] == 2
    assert result.data["interfaces"][1]["name"] == "Ethernet4"
    assert result.data["interfaces"][1]["rx_err"] == 5
    assert result.data["interfaces"][1]["rx_drp"] == 2


def test_get_device_config_truncates_with_metadata(monkeypatch):
    toolkit = _toolkit()

    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda container, cmd_args, timeout: subprocess.CompletedProcess(
            args=cmd_args,
            returncode=0,
            stdout="\n".join(f"line {index}" for index in range(5)),
            stderr="",
        ),
    )

    result = toolkit.get_device_config("leaf1", max_lines=2)

    assert result.success is True
    assert result.data["config"] == "line 0\nline 1"
    assert result.data["truncated"] is True
    assert result.data["returned_lines"] == 2
    assert result.data["total_lines"] == 5


def test_get_device_acl_respects_view(monkeypatch):
    toolkit = _toolkit()

    def fake_exec(container, cmd_args, timeout):
        command = " ".join(cmd_args)
        stdout = "iptables line" if "iptables" in command else "acl line"
        return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(toolkit, "_docker_exec", fake_exec)

    result = toolkit.get_device_acl("leaf1", view="iptables")

    assert result.success is True
    assert "iptables_forward_rules" in result.data
    assert "sonic_acl_config" not in result.data


def test_get_route_table_truncates_structured_routes(monkeypatch):
    toolkit = _toolkit()

    route_output = """B>* 10.0.0.0/24 [20/0] via 192.0.2.1, Ethernet0
B>* 10.0.1.0/24 [20/0] via 192.0.2.2, Ethernet4
"""
    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda container, cmd_args, timeout: subprocess.CompletedProcess(
            args=cmd_args,
            returncode=0,
            stdout=route_output,
            stderr="",
        ),
    )

    result = toolkit.get_route_table("leaf1", max_routes=1)

    assert result.success is True
    assert result.data["route_count"] == 2
    assert result.data["returned_routes"] == 1
    assert result.data["truncated"] is True
