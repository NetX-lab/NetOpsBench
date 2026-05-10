import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.scenario import generator as _generate_scenarios_mod
from netopsbench.platform.toolkit.toolkit import AgentToolkit


def test_parse_route_table_brief_output():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    sample = """
Codes: K - kernel route, C - connected, L - local, S - static,

IPv4 unicast VRF default:
S>* 192.168.2.2/32 [1/0] via 192.168.101.2, Ethernet8, weight 1, 01:14:52
B>* 192.168.102.0/30 [20/0] via 192.168.11.1, Ethernet0, weight 1, 01:30:50
  *                         via 192.168.21.1, Ethernet4, weight 1, 01:30:50
"""
    routes = toolkit._parse_route_table(sample)

    assert routes[0]["prefix"] == "192.168.2.2/32"
    assert routes[0]["protocol"] == "static"
    assert routes[0]["nexthops"] == [{"via": "192.168.101.2", "interface": "Ethernet8"}]

    assert routes[1]["prefix"] == "192.168.102.0/30"
    assert routes[1]["protocol"] == "bgp"
    assert routes[1]["nexthops"] == [
        {"via": "192.168.11.1", "interface": "Ethernet0"},
        {"via": "192.168.21.1", "interface": "Ethernet4"},
    ]


def test_parse_route_table_detailed_output():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    sample = """
Routing entry for 192.168.102.0/30
  Known via "bgp", distance 20, metric 0, best
  Last update 01:30:50 ago
  * 192.168.11.1, via Ethernet0, weight 1
  * 192.168.21.1, via Ethernet4, weight 1
"""
    routes = toolkit._parse_route_table(sample)

    assert routes == [
        {
            "prefix": "192.168.102.0/30",
            "code": None,
            "protocol": "bgp",
            "nexthops": [
                {"via": "192.168.11.1", "interface": "Ethernet0"},
                {"via": "192.168.21.1", "interface": "Ethernet4"},
            ],
            "admin_distance": 20,
            "metric": 0,
        }
    ]


def test_build_fault_instance_static_route_targets_remote_client():
    gs = _generate_scenarios_mod
    topo = gs.TopologyContext(
        scale="xs",
        topology_dir=Path("lab-topology/generated_topology_xs"),
        metadata={"scale": {"num_spines": 2}},
        spines=["spine1", "spine2"],
        leafs=["leaf1", "leaf2"],
        clients=[
            {"name": "client1", "leaf": "leaf1", "data_ip": "192.168.101.2"},
            {"name": "client2", "leaf": "leaf2", "data_ip": "192.168.102.2"},
        ],
        device_interfaces={},
        leaf_interface_roles={},
        client_interfaces={},
    )
    template = {"name": "static_route_misconfig", "device_role": "leaf", "severity": "medium"}
    defaults = {"seed": 42}

    scenario = gs.build_fault_instance(
        "static_route_misconfig",
        "hard",
        topo,
        random.Random(1),
        defaults,
        template,
        1,
    )

    episode = scenario["episodes"][1]
    assert episode["target_device"] == "leaf1"
    assert episode["metadata"]["target_ip"] == "192.168.102.2/32"
    assert episode["metadata"]["wrong_nexthop"] == "auto"


def test_recover_static_route_misconfig_prefers_specific_nexthop(monkeypatch):
    injector = FaultInjector(
        topology_metadata={
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.13"}],
                "clients": [
                    {"name": "client1", "leaf": "leaf1", "data_ip": "192.168.101.2"},
                    {"name": "client2", "leaf": "leaf2", "data_ip": "192.168.102.2"},
                ],
            }
        }
    )
    injector.active_faults = [
        {
            "type": "static_route_misconfig",
            "device": "leaf1",
            "target_ip": "192.168.102.2/32",
            "wrong_nexthop": "192.168.101.2",
        }
    ]

    captured = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_vtysh(device, commands):
        captured.append((device, commands))
        return Result()

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", fake_vtysh)

    result = injector.recover_static_route_misconfig(
        "leaf1",
        "192.168.102.2/32",
        "192.168.101.2",
    )

    assert result["recovered"] is True
    assert captured == [
        (
            "leaf1",
            [
                "configure terminal",
                "no ip route 192.168.102.2/32 192.168.101.2",
                "end",
                "write memory",
            ],
        )
    ]


def test_inject_static_route_misconfig_auto_uses_topology_clients(monkeypatch):
    injector = FaultInjector(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.13"}],
                "clients": [
                    {"name": "client1", "leaf": "leaf1", "data_ip": "192.168.101.2"},
                    {"name": "client2", "leaf": "leaf2", "data_ip": "192.168.102.2"},
                ],
            },
        }
    )

    class Result:
        returncode = 0
        stderr = ""

    captured = {}

    def fake_vtysh(device, commands):
        captured["device"] = device
        captured["commands"] = commands
        return Result()

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", fake_vtysh)

    result = injector.inject_static_route_misconfig(
        device="leaf1",
        target_ip="192.168.102.2/32",
        wrong_nexthop="auto",
    )

    assert result["success"] is True
    assert result["wrong_nexthop"] == "192.168.101.2"
    assert captured["device"] == "leaf1"
    assert "ip route 192.168.102.2/32 192.168.101.2" in captured["commands"]


def test_get_interface_mtu_falls_back_to_live_link(monkeypatch):
    injector = FaultInjector(
        topology_metadata={
            "devices": {
                "spines": [{"name": "spine1"}],
                "leafs": [],
                "clients": [],
            }
        }
    )

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_docker_exec(container, cmd, timeout=30):
        if cmd[:4] == ["sonic-db-cli", "CONFIG_DB", "hget", "PORT|Ethernet0"]:
            return Result(stdout="")
        if cmd == ["ip", "-o", "link", "show", "dev", "Ethernet0"]:
            return Result(stdout="9: Ethernet0: <UP> mtu 9100 qdisc mq state UP")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(injector._iface._cmd, "docker_exec", fake_docker_exec)

    assert injector._iface.get_interface_mtu("spine1", "Ethernet0") == 9100


def test_recover_mtu_mismatch_normalizes_invalid_saved_mtu(monkeypatch):
    injector = FaultInjector(
        topology_metadata={
            "devices": {
                "spines": [{"name": "spine1"}],
                "leafs": [],
                "clients": [],
            }
        }
    )

    captured = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_sonic_config(device, args):
        captured.append((device, args))
        return Result(returncode=0)

    def unexpected_docker_exec(*args, **kwargs):
        raise AssertionError("linux mtu fallback should not be used when normalized SONiC MTU succeeds")

    monkeypatch.setattr(injector._iface, "get_common_port_mtu", lambda device, exclude_interface=None: 9100)
    monkeypatch.setattr(injector._impairment._sonic, "config_cmd", fake_sonic_config)
    monkeypatch.setattr(injector._impairment._cmd, "docker_exec", unexpected_docker_exec)

    result = injector.recover_mtu_mismatch("spine1", "Ethernet0", 9500)

    assert result["recovered"] is True
    assert result["restored_mtu"] == 9100
    assert captured == [("spine1", ["interface", "mtu", "Ethernet0", "9100"])]


def test_topology_view_adds_mtu_semantics():
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "defaults": {"link_mtu": 9232},
            "devices": {"spines": [], "leafs": [], "clients": []},
            "links": [],
        }
    )

    topo = toolkit.get_topology()

    assert topo.success is True
    assert topo.data["defaults"]["link_mtu"] == 9232
    assert topo.data["defaults"]["sonic_port_mtu"] == 9100
    assert "9232" in topo.data["mtu_semantics"]["note"]
    assert "9100" in topo.data["mtu_semantics"]["note"]


def test_interface_metric_identities_cover_cli_and_gnmi_names():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

    identities = toolkit._resolve_interface_metric_identities("Ethernet8")

    assert identities["names"] == ["Ethernet8", "eth3"]
    assert identities["paths"] == ["COUNTERS/Ethernet8", "/COUNTERS/Ethernet8"]


def test_parse_influx_metric_rows_skips_repeated_headers():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    csv_text = """#group,false,false,true,true,true,true
#datatype,string,long,dateTime:RFC3339,double,string,string,string
,result,table,_time,_value,_field,path,source
,_result,0,2026-03-22T07:00:00Z,8,in_discarded_packets,/COUNTERS/Ethernet0,leaf1

,result,table,_time,_value,_field,name,path,source
,_result,1,2026-03-22T07:01:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
"""

    rows = toolkit._parse_influx_metric_rows(csv_text)

    assert rows == [
        {
            "_field": "in_discarded_packets",
            "_time": "2026-03-22T07:00:00Z",
            "_value": 8.0,
        },
        {
            "_field": "in_discarded_packets",
            "_time": "2026-03-22T07:01:00Z",
            "_value": 8.0,
        },
    ]


def test_interface_metric_summary_uses_window_delta_not_absolute_counter():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    points = [
        {"time": "2026-03-22T07:00:00Z", "value": 8.0},
        {"time": "2026-03-22T07:01:00Z", "value": 8.0},
    ]

    summary = toolkit._summarize_counter_points("in_discarded_packets", points)

    assert summary["counter_start"] == 8.0
    assert summary["counter_end"] == 8.0
    assert summary["window_delta"] == 0.0
    assert summary["elapsed_seconds"] == 60.0
    assert summary["avg_per_second"] == 0.0
    assert summary["points"] == 2


def test_parse_influx_timestamp_supports_nanosecond_precision():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

    parsed = toolkit._parse_influx_timestamp("2026-03-22T07:26:29.338351792Z")

    assert parsed is not None
    assert parsed.isoformat() == "2026-03-22T07:26:29.338351+00:00"


def test_parse_bgp_summary_skips_total_footer():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    sample = """
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
Total number of neighbors 1
"""

    rows = toolkit._parse_bgp_summary(sample)

    assert rows == [
        {
            "neighbor": "192.168.11.2",
            "asn": 65011,
            "state": "Established",
            "prefixes_received": 2,
            "up_down": "04:54:34",
            "msg_rcvd": 310,
            "msg_sent": 309,
            "in_q": 0,
            "out_q": 0,
        }
    ]


def test_parse_table_skips_separator_row():
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    sample = """
      IFACE    STATE      RX_OK    RX_BPS
-----------  -------  ---------  --------
  Ethernet0        U    318,237       N/A
"""

    rows = toolkit._parse_table(sample)

    assert rows == [{"IFACE": "Ethernet0", "STATE": "U", "RX_OK": "318,237", "RX_BPS": "N/A"}]


def test_get_interface_metrics_summary_reports_windowed_rates(monkeypatch):
    toolkit = AgentToolkit(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
    csv_text = """#group,false,false,true,true,true,true,true
#datatype,string,long,dateTime:RFC3339,double,string,string,string,string
,result,table,_time,_value,_field,path,source
,_result,0,2026-03-22T07:00:00Z,148118036,in_octets,/COUNTERS/Ethernet0,leaf1

,result,table,_time,_value,_field,name,path,source
,_result,1,2026-03-22T07:01:00Z,148546228,in_octets,Ethernet0,/COUNTERS/Ethernet0,leaf1
,_result,2,2026-03-22T07:00:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
,_result,2,2026-03-22T07:01:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
"""

    class FakeResponse:
        status_code = 200
        text = csv_text

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    result = toolkit.get_interface_metrics("leaf1", "Ethernet0", time_range_minutes=5)

    assert result.success is True
    assert result.data["summary"]["in_discarded_packets"]["window_delta"] == 0.0
    assert result.data["summary"]["in_octets"]["window_delta"] == 428192.0
    assert result.data["summary"]["in_octets"]["avg_per_second"] == pytest.approx(7136.533333333334)
    assert result.data["summary"]["in_octets"]["avg_bps"] == pytest.approx(57092.26666666667)


def test_get_interface_metrics_attaches_live_snapshot_when_influx_missing(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.15"}],
                "clients": [],
            },
        }
    )

    class EmptyResponse:
        status_code = 200
        text = ""

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.requests.post",
        lambda *args, **kwargs: EmptyResponse(),
    )
    monkeypatch.setattr(
        toolkit,
        "get_device_interfaces",
        lambda device, format="structured": type(
            "Result",
            (),
            {
                "success": True,
                "data": {
                    "interfaces": [
                        {"name": "Ethernet0", "oper": "up", "rx_ok": 1234, "tx_ok": 5678},
                    ]
                },
            },
        )(),
    )

    result = toolkit.get_interface_metrics("leaf1", "Ethernet0", time_range_minutes=5)

    assert result.success is True
    assert result.data["summary"] == {}
    assert result.data["current_snapshot"]["name"] == "Ethernet0"
    assert result.data["fallback_source"] == "live_cli_snapshot"
    assert result.data["observed_interfaces"] == []
    assert result.data["active_interfaces"] == ["Ethernet0"]
    assert result.data["missing_active_interfaces"] == ["Ethernet0"]
    assert "fallback" in result.data["warning"].lower()


def test_get_interface_metrics_reports_recent_interface_coverage_gap(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.15"}],
                "clients": [],
            },
        }
    )

    class FakeResponse:
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    responses = iter(
        [
            FakeResponse(""),
            FakeResponse("""#group,false,false,true,true,true
#datatype,string,long,dateTime:RFC3339,string,string
,result,table,_time,name,path
,_result,0,2026-03-23T14:00:00Z,Ethernet24,/COUNTERS/Ethernet24
"""),
        ]
    )

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.requests.post",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        toolkit,
        "get_device_interfaces",
        lambda device, format="structured": type(
            "Result",
            (),
            {
                "success": True,
                "data": {
                    "interfaces": [
                        {"name": "Ethernet0", "oper": "up", "rx_ok": 1234, "tx_ok": 5678},
                        {"name": "Ethernet4", "oper": "up", "rx_ok": 4321, "tx_ok": 8765},
                    ]
                },
            },
        )(),
    )

    result = toolkit.get_interface_metrics("leaf1", "Ethernet0", time_range_minutes=5)

    assert result.success is True
    assert result.data["observed_interfaces"] == ["Ethernet24"]
    assert result.data["active_interfaces"] == ["Ethernet0", "Ethernet4"]
    assert result.data["missing_active_interfaces"] == ["Ethernet0", "Ethernet4"]
    assert "Ethernet24" in result.data["warning"]


def test_get_device_logs_falls_back_to_container_logs(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.15"}],
                "clients": [],
            },
        }
    )

    class EmptyResponse:
        status_code = 200
        text = ""

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.log_ops.requests.post",
        lambda *args, **kwargs: EmptyResponse(),
    )
    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda container, cmd_args, timeout: subprocess.CompletedProcess(
            args=["docker", "exec", container] + list(cmd_args),
            returncode=0,
            stdout=(f"{datetime.now(UTC):%b %d %H:%M:%S.%f} " "leaf1 NOTICE #root: fallback-message\n"),
            stderr="",
        ),
    )

    result = toolkit.get_device_logs("leaf1", time_range_minutes=10)

    assert result.success is True
    assert result.data["source"] == "container_logs_fallback"
    assert result.data["logs"][0]["message"] == "fallback-message"
    assert result.data["logs"][0]["severity"] == "notice"


def test_ping_test_allows_infra_source(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [{"name": "spine1", "mgmt_ip": "172.20.20.11"}],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.12"}],
                "clients": [
                    {"name": "client1", "mgmt_ip": "172.20.20.21", "data_ip": "192.168.101.2"},
                    {"name": "client2", "mgmt_ip": "172.20.20.22", "data_ip": "192.168.102.2"},
                ],
            },
        }
    )

    class Result:
        returncode = 0
        stdout = "PING 192.168.102.2: 5 packets transmitted, 5 received\n"
        stderr = ""

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.validators.subprocess.run",
        lambda *a, **kw: Result(),
    )

    result = toolkit.ping_test("leaf1", "192.168.102.2")
    assert result.success is True


def test_traceroute_allows_infra_source(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [{"name": "spine1", "mgmt_ip": "172.20.20.11"}],
                "leafs": [{"name": "leaf1", "mgmt_ip": "172.20.20.12"}],
                "clients": [
                    {"name": "client1", "mgmt_ip": "172.20.20.21", "data_ip": "192.168.101.2"},
                    {"name": "client2", "mgmt_ip": "172.20.20.22", "data_ip": "192.168.102.2"},
                ],
            },
        }
    )

    class Result:
        returncode = 0
        stdout = "traceroute to 192.168.102.2, 30 hops max\n"
        stderr = ""

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.validators.subprocess.run",
        lambda *a, **kw: Result(),
    )

    result = toolkit.traceroute("spine1", "192.168.102.2")
    assert result.success is True


def test_ping_test_allows_client_source(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [],
                "clients": [
                    {"name": "client1", "mgmt_ip": "172.20.20.21", "data_ip": "192.168.101.2"},
                    {"name": "client2", "mgmt_ip": "172.20.20.22", "data_ip": "192.168.102.2"},
                ],
            },
        }
    )

    class Result:
        returncode = 0
        stdout = (
            "3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n"
            "rtt min/avg/max/mdev = 1.856/2.078/2.193/0.157 ms\n"
        )
        stderr = ""

    captured = []

    def fake_docker_exec(container, cmd, timeout):
        captured.append((container, cmd, timeout))
        return Result()

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.ping_test("client1", "192.168.102.2", 3)

    assert result.success is True
    assert result.data["source"] == "client1"
    assert result.data["payload_size"] is None
    assert result.data["dont_fragment"] is False
    assert captured == [("clab-dcn-client1", ["ping", "-c", "3", "-W", "2", "192.168.102.2"], 30)]


def test_ping_test_supports_large_packet_df_probe(monkeypatch):
    toolkit = AgentToolkit(
        topology_metadata={
            "name": "dcn",
            "devices": {
                "spines": [],
                "leafs": [],
                "clients": [
                    {"name": "client1", "mgmt_ip": "172.20.20.21", "data_ip": "192.168.101.2"},
                    {"name": "client2", "mgmt_ip": "172.20.20.22", "data_ip": "192.168.102.2"},
                ],
            },
        }
    )

    class Result:
        returncode = 1
        stdout = "ping: local error: message too long, mtu=1400\n"
        stderr = ""

    captured = []

    def fake_docker_exec(container, cmd, timeout):
        captured.append((container, cmd, timeout))
        return Result()

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.ping_test(
        "client1",
        "192.168.102.2",
        count=2,
        payload_size=2000,
        dont_fragment=True,
    )

    assert result.success is True
    assert result.data["payload_size"] == 2000
    assert result.data["dont_fragment"] is True
    assert captured == [
        (
            "clab-dcn-client1",
            ["ping", "-c", "2", "-W", "2", "-s", "2000", "-M", "do", "192.168.102.2"],
            30,
        )
    ]
