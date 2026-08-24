import random
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
from netopsbench.platform.observability.influxdb import FluxQueryResult
from netopsbench.platform.scenario import generator as _generate_scenarios_mod
from netopsbench.platform.toolkit._core.device.connectivity_ops import ConnectivityOpsMixin
from netopsbench.platform.toolkit._core.device.interface_parsers import parse_ip_link_stats
from netopsbench.platform.toolkit._core.device.route_parsers import parse_route_table
from netopsbench.platform.toolkit._core.device.telemetry_parsers import (
    parse_influx_metric_rows,
    parse_influx_timestamp,
    query_influx,
    summarize_counter_points,
)
from netopsbench.platform.toolkit._core.device.text_parsers import parse_table
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.utils.interface_names import resolve_interface_metric_identities


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def test_parse_ip_link_stats_removes_transient_veth_peer_suffix():
    parsed = parse_ip_link_stats("2: eth1@if123: <BROADCAST,MULTICAST,UP> mtu 1500 state UP\n")

    assert parsed == [{"name": "eth1", "mtu": 1500}]


def test_one_way_latency_capture_matches_icmp_sequences():
    output = """\
1723456789.100000 IP 10.0.0.1 > 10.0.0.2: ICMP echo request, id 4, seq 1, length 64
1723456789.250000 IP 10.0.0.1 > 10.0.0.2: ICMP echo request, id 4, icmp_seq=2, length 64
"""

    assert ConnectivityOpsMixin._icmp_capture_timestamps(output) == {
        (4, 1): 1723456789.1,
        (4, 2): 1723456789.25,
    }


def test_latency_link_test_uses_addressed_l3_source(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())

    def fake_docker_exec(container, cmd, timeout):
        if cmd[0] == "ip":
            address = "10.1.1.1" if container.endswith("spine1") else "10.1.1.2"
            return subprocess.CompletedProcess(cmd, 0, f"1: Ethernet0 inet {address}/31 scope global\n", "")
        if "tcpdump" in cmd:
            source = cmd[cmd.index("src") + 2]
            target = cmd[cmd.index("dst") + 2]
            source_side = container.endswith("spine1") if source == "10.1.1.1" else container.endswith("leaf1")
            timestamp = 1723456789.1 + (0 if source_side else 0.12)
            output = (
                f"{timestamp:.6f} eth0 Out IP (proto ICMP (1), length 84)\n"
                f"    {source} > {target}: ICMP echo request, id 7, seq 1, length 64\n"
            )
            return subprocess.CompletedProcess(cmd, 0, output, "")
        return subprocess.CompletedProcess(cmd, 0, "1 packets transmitted, 1 received, 0% packet loss\n", "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.latency_link_test("spine1", "Ethernet0", "leaf1", "Ethernet0", count=3)

    assert result.success is True
    assert result.data["directions"][0]["median_ms"] == pytest.approx(120.0, abs=0.001)


def test_parse_route_table_brief_output():
    sample = """
Codes: K - kernel route, C - connected, L - local, S - static,

IPv4 unicast VRF default:
S>* 192.168.2.2/32 [1/0] via 192.168.101.2, Ethernet8, weight 1, 01:14:52
B>* 192.168.102.0/30 [20/0] via 192.168.11.1, Ethernet0, weight 1, 01:30:50
  *                         via 192.168.21.1, Ethernet4, weight 1, 01:30:50
"""
    routes = parse_route_table(sample)

    assert routes[0]["prefix"] == "192.168.2.2/32"
    assert routes[0]["protocol"] == "static"
    assert routes[0]["nexthops"] == [{"via": "192.168.101.2", "interface": "Ethernet8"}]
    assert routes[0]["selected"] is True
    assert routes[0]["is_discard"] is False

    assert routes[1]["prefix"] == "192.168.102.0/30"
    assert routes[1]["protocol"] == "bgp"
    assert routes[1]["nexthops"] == [
        {"via": "192.168.11.1", "interface": "Ethernet0"},
        {"via": "192.168.21.1", "interface": "Ethernet4"},
    ]


def test_parse_route_table_detailed_output():
    sample = """
Routing entry for 192.168.102.0/30
  Known via "bgp", distance 20, metric 0, best
  Last update 01:30:50 ago
  * 192.168.11.1, via Ethernet0, weight 1
  * 192.168.21.1, via Ethernet4, weight 1
"""
    routes = parse_route_table(sample)

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
            "selected": True,
            "is_discard": False,
            "discard_interface": None,
        }
    ]


def test_parse_route_table_accepts_lowercase_frr_status_flags():
    routes = parse_route_table("S>q 192.168.109.0/30 [1/0] via 192.168.115.2, Ethernet16, weight 1\n")

    assert routes[0]["protocol"] == "static"
    assert routes[0]["selected"] is True
    assert routes[0]["nexthops"] == [{"via": "192.168.115.2", "interface": "Ethernet16"}]


def test_parse_route_table_detailed_static_route_uses_active_nexthop_as_selected():
    routes = parse_route_table(
        """
Routing entry for 192.168.109.0/30
  Known via "static", distance 1, metric 0
  * 192.168.115.2, via Ethernet16, weight 1
"""
    )

    assert routes[0]["protocol"] == "static"
    assert routes[0]["selected"] is True
    assert routes[0]["nexthops"] == [{"via": "192.168.115.2", "interface": "Ethernet16"}]


def test_parse_route_table_marks_null0_as_discard_route():
    sample = """
Routing entry for 192.168.105.0/30
  Known via "static", distance 1, metric 0, best
  * directly connected, Null0
"""

    routes = parse_route_table(sample)

    assert routes == [
        {
            "prefix": "192.168.105.0/30",
            "code": None,
            "protocol": "static",
            "nexthops": [{"via": None, "interface": "Null0"}],
            "admin_distance": 1,
            "metric": 0,
            "selected": True,
            "is_discard": True,
            "discard_interface": "Null0",
        }
    ]


def test_parse_route_table_preserves_blackhole_discard_kind():
    sample = "S>* 192.168.105.0/30 [1/0] blackhole, weight 1, 00:00:10"

    routes = parse_route_table(sample)

    assert routes[0]["protocol"] == "static"
    assert routes[0]["selected"] is True
    assert routes[0]["is_discard"] is True
    assert routes[0]["discard_interface"] == "blackhole"


def test_build_fault_instance_static_route_targets_remote_client(tmp_path):
    gs = _generate_scenarios_mod
    topology_dir = tmp_path / "topology"
    generate_topology("xs", str(topology_dir))
    topo = gs.load_topology("xs", str(topology_dir))
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

    episode = scenario["episode"]
    assert episode["target_device"] == "leaf1"
    assert episode["metadata"]["target_ip"] == "192.168.102.2/32"
    assert episode["metadata"]["wrong_nexthop"] == "auto"


def test_recover_static_route_misconfig_prefers_specific_nexthop(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
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
        stdout = ""

    def fake_vtysh(device, commands):
        if commands == ["show running-config"]:
            return Result()
        if commands == ["show ip route 192.168.102.2/32"]:
            return Result()
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
    injector = FaultInjector(topology_metadata=_metadata())

    class Result:
        returncode = 0
        stderr = ""
        stdout = "ip route 192.168.102.2/32 192.168.101.2\n"

    captured = []

    def fake_vtysh(device, commands):
        if commands == ["show running-config"]:
            return Result()
        if commands == ["show ip route 192.168.102.2/32"]:
            return Result()
        captured.append((device, commands))
        return Result()

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", fake_vtysh)

    result = injector.inject_static_route_misconfig(
        device="leaf1",
        target_ip="192.168.102.2/32",
        wrong_nexthop="auto",
    )

    assert result["success"] is True
    assert result["wrong_nexthop"] == "192.168.101.2"
    assert captured == [
        (
            "leaf1",
            [
                "configure terminal",
                "ip route 192.168.102.2/32 192.168.101.2",
                "end",
                "write memory",
            ],
        )
    ]


def test_get_interface_mtu_uses_effective_live_link(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())

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


def test_get_interface_mtu_does_not_hide_live_drift_with_config_db(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_docker_exec(_container, cmd, timeout=30):
        if cmd == ["ip", "-o", "link", "show", "dev", "Ethernet0"]:
            return Result(stdout="9: Ethernet0: <UP> mtu 1500 qdisc mq state UP")
        if cmd[:4] == ["sonic-db-cli", "CONFIG_DB", "hget", "PORT|Ethernet0"]:
            return Result(stdout="9100")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(injector._iface._cmd, "docker_exec", fake_docker_exec)

    assert injector._iface.get_interface_mtu("spine1", "Ethernet0") == 1500


def test_mtu_handler_applies_live_fallback_when_config_db_does_not_converge(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    effective = iter([9100, 9100, 1400])
    fallback_commands = []
    monkeypatch.setattr(
        injector._impairment._iface,
        "get_interface_mtu",
        lambda _device, _interface: next(effective),
    )
    monkeypatch.setattr(
        injector._impairment._sonic,
        "config_cmd",
        lambda _device, _args: Result(),
    )
    monkeypatch.setattr(
        injector._impairment._cmd,
        "docker_exec",
        lambda _container, command: fallback_commands.append(command) or Result(),
    )

    result = injector.inject_mtu_mismatch("spine1", "Ethernet0", mtu=1400)

    assert result["success"] is True
    assert result["original_mtu"] == 9100
    assert fallback_commands == [["ip", "link", "set", "eth1", "mtu", "1400"]]


def test_netem_readback_compares_normalized_numeric_values(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_docker_exec(_container, command, timeout=30):
        if command[:4] == ["tc", "qdisc", "replace", "dev"]:
            return Result()
        if command == ["tc", "qdisc", "show", "dev", "eth1"]:
            return Result(stdout="qdisc netem 8001: root refcnt 2 limit 1000 loss 10%")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(injector._impairment._cmd, "docker_exec", fake_docker_exec)

    result = injector.inject_packet_loss("spine1", "Ethernet0", loss_pct=10.0)

    assert result["success"] is True


def test_recover_mtu_mismatch_normalizes_invalid_saved_mtu(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())

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
    monkeypatch.setattr(injector._iface, "get_interface_mtu", lambda device, interface: 9100)
    monkeypatch.setattr(injector._impairment._sonic, "config_cmd", fake_sonic_config)
    monkeypatch.setattr(injector._impairment._cmd, "docker_exec", unexpected_docker_exec)

    result = injector.recover_mtu_mismatch("spine1", "Ethernet0", 9500)

    assert result["recovered"] is True
    assert result["restored_mtu"] == 9100
    assert captured == [("spine1", ["interface", "mtu", "Ethernet0", "9100"])]


def test_topology_view_adds_mtu_semantics():
    toolkit = AgentToolkit(topology_metadata=_metadata())

    topo = toolkit.get_topology()

    assert topo.success is True
    assert topo.data["defaults"]["link_mtu"] == 9232
    assert topo.data["defaults"]["sonic_port_mtu"] == 9100
    assert "9232" in topo.data["mtu_semantics"]["note"]
    assert "9100" in topo.data["mtu_semantics"]["note"]


def test_interface_metric_identities_cover_cli_and_gnmi_names():
    identities = resolve_interface_metric_identities("Ethernet8")

    assert identities["names"] == ["Ethernet8", "e1-3", "eth3", "ethernet-1/3"]
    assert identities["paths"] == ["COUNTERS/Ethernet8", "/COUNTERS/Ethernet8"]


def test_parse_influx_metric_rows_skips_repeated_headers():
    csv_text = """#group,false,false,true,true,true,true
#datatype,string,long,dateTime:RFC3339,double,string,string,string
,result,table,_time,_value,_field,path,source
,_result,0,2026-03-22T07:00:00Z,8,in_discarded_packets,/COUNTERS/Ethernet0,leaf1

,result,table,_time,_value,_field,name,path,source
,_result,1,2026-03-22T07:01:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
"""

    rows = parse_influx_metric_rows(csv_text)

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
    points = [
        {"time": "2026-03-22T07:00:00Z", "value": 8.0},
        {"time": "2026-03-22T07:01:00Z", "value": 8.0},
    ]

    summary = summarize_counter_points("in_discarded_packets", points)

    assert summary["counter_start"] == 8.0
    assert summary["counter_end"] == 8.0
    assert summary["window_delta"] == 0.0
    assert summary["elapsed_seconds"] == 60.0
    assert summary["avg_per_second"] == 0.0
    assert summary["points"] == 2


def test_parse_influx_timestamp_supports_nanosecond_precision():
    parsed = parse_influx_timestamp("2026-03-22T07:26:29.338351792Z")

    assert parsed is not None
    assert parsed.isoformat() == "2026-03-22T07:26:29.338351+00:00"


def test_influx_query_failure_is_structured_not_empty_data(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.telemetry_parsers.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="error", error="ConnectionError: offline"),
    )

    result = query_influx(toolkit, 'from(bucket: "test")')

    assert result.status == "error"
    assert result.rows == []
    assert "offline" in result.error


def test_query_influx_parses_multiple_yield_headers_and_keeps_zero(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    csv_text = """#datatype,string,long,string,long
,result,table,source,_value
,index_count,0,leaf1,5

#datatype,string,long,dateTime:RFC3339,double,string
,result,table,_time,_value,source
,index_last,1,2026-07-11T00:00:20Z,0,leaf1
"""
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.telemetry_parsers.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="ok", text=csv_text),
    )

    result = query_influx(toolkit, 'from(bucket: "test")')

    assert result.status == "ok"
    assert result.rows == [
        {"": "", "result": "index_count", "table": 0.0, "source": "leaf1", "_value": 5.0},
        {
            "": "",
            "result": "index_last",
            "table": 1.0,
            "_time": "2026-07-11T00:00:20Z",
            "_value": 0.0,
            "source": "leaf1",
        },
    ]


def test_parse_bgp_summary_skips_total_footer():
    sample = """
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
Total number of neighbors 1
"""

    rows = parse_bgp_summary(sample)

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
    sample = """
      IFACE    STATE      RX_OK    RX_BPS
-----------  -------  ---------  --------
  Ethernet0        U    318,237       N/A
"""

    rows = parse_table(sample)

    assert rows == [{"IFACE": "Ethernet0", "STATE": "U", "RX_OK": "318,237", "RX_BPS": "N/A"}]


def test_get_interface_metrics_summary_reports_windowed_rates(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    csv_text = """#group,false,false,true,true,true,true,true
#datatype,string,long,dateTime:RFC3339,double,string,string,string,string
,result,table,_time,_value,_field,path,source
,_result,0,2026-03-22T07:00:00Z,148118036,in_octets,/COUNTERS/Ethernet0,leaf1

,result,table,_time,_value,_field,name,path,source
,_result,1,2026-03-22T07:01:00Z,148546228,in_octets,Ethernet0,/COUNTERS/Ethernet0,leaf1
,_result,2,2026-03-22T07:00:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
,_result,2,2026-03-22T07:01:00Z,8,in_discarded_packets,Ethernet0,/COUNTERS/Ethernet0,leaf1
"""

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="ok", text=csv_text),
    )

    result = toolkit.get_interface_metrics("leaf1", "Ethernet0", time_range_minutes=5)

    assert result.success is True
    assert result.data["summary"]["in_discarded_packets"]["window_delta"] == 0.0
    assert result.data["summary"]["in_octets"]["window_delta"] == 428192.0
    assert result.data["summary"]["in_octets"]["avg_per_second"] == pytest.approx(7136.533333333334)
    assert result.data["summary"]["in_octets"]["avg_bps"] == pytest.approx(57092.26666666667)


def test_get_interface_metrics_attaches_live_snapshot_when_influx_missing(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="ok"),
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
    toolkit = AgentToolkit(topology_metadata=_metadata())

    identity_result = FluxQueryResult(
        status="ok",
        text="""#group,false,false,true,true,true
#datatype,string,long,dateTime:RFC3339,string,string
,result,table,_time,name,path
,_result,0,2026-03-23T14:00:00Z,Ethernet24,/COUNTERS/Ethernet24
""",
    )

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.observability.metrics_ops.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="ok"),
    )
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.telemetry_parsers.query_flux",
        lambda *args, **kwargs: identity_result,
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
    toolkit = AgentToolkit(topology_metadata=_metadata())

    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.log_ops.query_flux",
        lambda *args, **kwargs: FluxQueryResult(status="ok"),
    )
    monkeypatch.setattr(toolkit, "_resolve_container", lambda device: "clab-dcn-leaf1")
    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda container, cmd_args, timeout: subprocess.CompletedProcess(
            args=["docker", "exec", container] + list(cmd_args),
            returncode=0,
            stdout=(f"{datetime.now(UTC):%b %d %H:%M:%S.%f} leaf1 NOTICE #root: fallback-message\n"),
            stderr="",
        ),
    )

    result = toolkit.get_device_logs("leaf1", time_range_minutes=10)

    assert result.success is True
    assert result.data["source"] == "container_logs_fallback"
    assert result.data["logs"][0]["message"] == "fallback-message"
    assert result.data["logs"][0]["severity"] == "notice"


def test_container_log_fallback_respects_episode_end_time(monkeypatch):
    from netopsbench.platform.toolkit._core.device.log_parsers import parse_local_syslog_lines

    now = datetime.now(UTC).replace(microsecond=0)
    before = now - timedelta(seconds=10)
    inside = now
    at_end = now + timedelta(seconds=10)
    lines = "\n".join(
        [
            f"{before:%b %d %H:%M:%S} leaf1 NOTICE #root: before",
            f"{inside:%b %d %H:%M:%S} leaf1 NOTICE #root: inside",
            f"{at_end:%b %d %H:%M:%S} leaf1 NOTICE #root: at-end",
        ]
    )

    parsed = parse_local_syslog_lines(lines, cutoff=inside, end_time=at_end)

    assert [item["message"] for item in parsed] == ["inside"]


def test_ping_test_allows_infra_source(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())

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


def test_ping_link_test_resolves_peer_ip_and_binds_front_panel(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    calls = []

    def fake_docker_exec(container, cmd, timeout):
        calls.append((container, cmd, timeout))
        if cmd[:5] == ["ip", "-4", "-o", "address", "show"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "3: Ethernet0 inet 10.1.1.2/30 scope global Ethernet0\n",
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "1 packets transmitted, 1 received, 0% packet loss\n", "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.ping_link_test("spine1", "leaf1", "eth1", "eth1", count=1)

    assert result.success is True
    assert result.data["destination"] == "10.1.1.2"
    assert result.data["source_interface"] == "Ethernet0"
    assert calls[-1][1] == ["ping", "-c", "1", "-W", "2", "-I", "Ethernet0", "10.1.1.2"]


def test_payload_integrity_link_test_captures_both_directions(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    calls = []

    def fake_docker_exec(container, cmd, timeout):
        calls.append((container, cmd, timeout))
        if cmd[0] == "ip":
            address = "10.1.1.1" if container.endswith("spine1") else "10.1.1.2"
            return subprocess.CompletedProcess(cmd, 0, f"1: Ethernet0 inet {address}/31 scope global\n", "")
        if "tcpdump" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "20 packets captured\n20 packets received by filter\n0 packets dropped by kernel\n",
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "20 packets transmitted, 20 received, 0% packet loss\n", "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.payload_integrity_link_test(
        "spine1",
        "Ethernet0",
        "leaf1",
        "Ethernet0",
        count=20,
    )

    assert result.success is True
    assert len(result.data["directions"]) == 2
    assert all(item["packets_sent"] == 20 for item in result.data["directions"])
    assert len([item for item in calls if "tcpdump" in item[1]]) == 2


@pytest.mark.parametrize(("source", "role"), [("spine1", "spine"), ("leaf1", "leaf")])
def test_traceroute_rejects_infra_source_without_docker_exec(monkeypatch, source, role):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    calls = []

    monkeypatch.setattr(toolkit, "_docker_exec", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = toolkit.traceroute(source, "192.168.102.2")

    assert result.success is False
    assert result.data is None
    assert result.error == f"Traceroute source must be a client device, got {source} ({role})"
    assert calls == []


def test_traceroute_uses_bounded_probe_budget(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    calls = []

    def fake_docker_exec(container, cmd, timeout):
        calls.append((container, cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0, "1  *\n2  192.168.102.2  1.1 ms\n", "")

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.traceroute("client1", "192.168.102.2")

    assert result.success is True
    assert result.data["traceroute"].startswith("1  *")
    assert calls == [
        (
            toolkit.container_names["client1"],
            ["traceroute", "-n", "-q", "1", "-w", "1", "-m", "8", "192.168.102.2"],
            12,
        )
    ]


def test_traceroute_preserves_timeout_error_contract(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())

    def fake_docker_exec(container, cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(toolkit, "_docker_exec", fake_docker_exec)

    result = toolkit.traceroute("client1", "192.168.102.2")

    assert result.success is False
    assert result.data is None
    assert result.error == "Traceroute timed out"


def test_traceroute_reports_nonzero_command_exit(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())
    cmd = ["traceroute", "-n", "192.168.102.2"]

    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            cmd,
            127,
            'OCI runtime exec failed: exec: "traceroute": executable file not found',
            "",
        ),
    )

    result = toolkit.traceroute("client1", "192.168.102.2")

    assert result.success is False
    assert result.data is None
    assert result.error == (
        "Traceroute failed on client1 (exit 127): "
        'OCI runtime exec failed: exec: "traceroute": executable file not found'
    )


def test_ping_test_allows_client_source(monkeypatch):
    toolkit = AgentToolkit(topology_metadata=_metadata())

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
    toolkit = AgentToolkit(topology_metadata=_metadata())

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
