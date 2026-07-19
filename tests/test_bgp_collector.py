from pathlib import Path

from netopsbench.models import topology as topology_models
from netopsbench.models.topology import Collector, Device, DeviceRole, Management, TopologyManifest
from netopsbench.platform.observability.bgp_collector import (
    BgpTransitionTracker,
    _collect_bgp_lines_paced,
    _write_lines,
    build_bgp_collection_line,
    build_bgp_lines,
    collect_bgp_lines,
    normalize_bgp_state,
    run_once,
)


def _write_topology(
    path: Path,
    *,
    name: str = "demo",
    family: str = "clos",
    devices: list[Device] | None = None,
) -> None:
    devices = devices or [
        Device(name="spine1", role=DeviceRole.SPINE),
        Device(name="leaf1", role=DeviceRole.LEAF),
    ]
    manifest = TopologyManifest(
        topology_id=name,
        name=name,
        scale="xs" if family == "clos" else "fat-tree-k8",
        family=family,
        management=Management(network=f"clab-{name}", ipv4_subnet="172.20.20.0/24"),
        collector=Collector(ipv4="172.20.20.200"),
        defaults=topology_models.TopologyDefaults(),
        facts=topology_models.TopologyFacts(
            num_spines=sum(device.role is DeviceRole.SPINE for device in devices),
            num_leafs=sum(device.role is DeviceRole.LEAF for device in devices),
            num_cores=sum(device.role is DeviceRole.CORE for device in devices),
            num_aggs=sum(device.role is DeviceRole.AGG for device in devices),
            num_edges=sum(device.role is DeviceRole.EDGE for device in devices),
            num_pods=2 if family == "fat-tree" else 0,
            clients_per_attached_switch=1,
            total_clients=sum(device.role is DeviceRole.CLIENT for device in devices),
            total_switches=sum(device.role is not DeviceRole.CLIENT for device in devices),
            fat_tree_k=2 if family == "fat-tree" else None,
            full_density_clients_per_attached_switch=1 if family == "fat-tree" else None,
            host_density="standard" if family == "fat-tree" else None,
        ),
        routing=topology_models.RoutingMetadata(
            ecmp_hash_policy_by_role={device.role: 1 for device in devices if device.role is not DeviceRole.CLIENT}
        ),
        devices=devices,
        links=[],
    )
    path.write_text(manifest.model_dump_json(), encoding="utf-8")


def test_build_bgp_lines_normalizes_states_and_fields():
    lines = build_bgp_lines(
        "spine1",
        [
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
        ],
        123456789,
        topology_id="xs lab",
    )

    assert len(lines) == 1
    assert lines[0].startswith("bgp_neighbors,source=spine1,neighbor_address=192.168.11.2,topology_id=xs\\ lab ")
    assert 'session_state="ESTABLISHED"' in lines[0]
    assert "asn=65011i" in lines[0]
    assert "prefixes_received=2i" in lines[0]
    assert lines[0].endswith(" 123456789")


def test_normalize_bgp_state_defaults_to_unknown():
    assert normalize_bgp_state(None) == "UNKNOWN"
    assert normalize_bgp_state("Idle") == "IDLE"


def test_build_bgp_collection_line_records_success_and_failure():
    success = build_bgp_collection_line("leaf1", 7, "runtime-xs", True, 2, 41, "")
    failure = build_bgp_collection_line("leaf1", 8, "runtime-xs", False, 0, 30000, "timeout")

    assert success.startswith("bgp_collection,source=leaf1,topology_id=runtime-xs ")
    assert "collection_ok=true" in success
    assert "neighbor_count=2i" in success
    assert 'error_type="timeout"' in failure
    assert "collection_ok=false" in failure


def test_bgp_transition_tracker_baselines_then_emits_down_and_recovery():
    tracker = BgpTransitionTracker()
    established = [{"neighbor": "10.0.0.1", "state": "Established", "asn": 65100, "prefixes_received": 4}]
    idle = [{"neighbor": "10.0.0.1", "state": "Idle", "asn": 65100}]

    baseline = tracker.process("leaf1", established, 1, "runtime-xs", collection_ok=True)
    down = tracker.process("leaf1", idle, 2, "runtime-xs", collection_ok=True)
    recovered = tracker.process("leaf1", established, 3, "runtime-xs", collection_ok=True)

    assert len(baseline) == 1
    assert baseline[0].startswith("bgp_event_index,source=leaf1,topology_id=runtime-xs ")
    assert "schema_version=1i" in baseline[0]
    assert any(",event_type=session_down," in line for line in down)
    assert any('previous_state="ESTABLISHED"' in line and 'latest_state="IDLE"' in line for line in down)
    assert any(",event_type=session_recovered," in line for line in recovered)


def test_bgp_transition_tracker_distinguishes_missing_peer_from_collection_failure():
    tracker = BgpTransitionTracker()
    established = [{"neighbor": "10.0.0.1", "state": "Established", "prefixes_received": 4}]
    tracker.process("leaf1", established, 1, "runtime-xs", collection_ok=True)

    failed = tracker.process("leaf1", [], 2, "runtime-xs", collection_ok=False)
    recovered_without_transition = tracker.process("leaf1", established, 3, "runtime-xs", collection_ok=True)
    missing = tracker.process("leaf1", [], 4, "runtime-xs", collection_ok=True)

    assert len(failed) == 1
    assert "collection_ok=false" in failed[0]
    assert len(recovered_without_transition) == 1
    assert any(",event_type=session_down," in line and 'latest_state="MISSING"' in line for line in missing)


def test_bgp_transition_tracker_restart_rebuilds_baseline_without_false_event():
    restarted = BgpTransitionTracker()

    lines = restarted.process(
        "leaf1",
        [{"neighbor": "10.0.0.1", "state": "Idle", "asn": 65100}],
        9,
        "runtime-xs",
        collection_ok=True,
    )

    assert len(lines) == 1
    assert lines[0].startswith("bgp_event_index,")


def test_collect_bgp_lines_reads_topology_and_executes_docker(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(metadata_file)

    calls = []

    class _Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(args, capture_output, text, check, timeout):
        calls.append(args)
        return _Result("""
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
""")

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.subprocess.run", fake_run)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])

    lines = collect_bgp_lines(Path(metadata_file), timestamp_ns=7, topology_id="runtime-xs")

    assert len(lines) == 4
    assert calls[0][:3] == ["docker", "exec", "clab-demo-spine1"]
    assert calls[1][:3] == ["docker", "exec", "clab-demo-leaf1"]
    assert sum(line.startswith("bgp_neighbors,") for line in lines) == 2
    assert sum(line.startswith("bgp_collection,") for line in lines) == 2
    assert all(",topology_id=runtime-xs " in line for line in lines)


def test_collect_bgp_lines_supports_parallelism(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(
        metadata_file,
        devices=[
            Device(name="spine1", role=DeviceRole.SPINE),
            Device(name="spine2", role=DeviceRole.SPINE),
            Device(name="leaf1", role=DeviceRole.LEAF),
        ],
    )

    calls = []

    class _Result:
        returncode = 0
        stdout = """
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
"""

    def fake_run(args, capture_output, text, check, timeout):
        calls.append(args[2])
        return _Result()

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.subprocess.run", fake_run)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])

    lines = collect_bgp_lines(Path(metadata_file), timestamp_ns=9, parallelism=2)

    assert len(lines) == 6
    assert sorted(calls) == ["clab-demo-leaf1", "clab-demo-spine1", "clab-demo-spine2"]
    assert all(line.endswith(" 9") for line in lines)


def test_loop_collection_spreads_device_starts_over_interval(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(
        metadata_file,
        devices=[
            Device(name="spine1", role=DeviceRole.SPINE),
            Device(name="spine2", role=DeviceRole.SPINE),
            Device(name="leaf1", role=DeviceRole.LEAF),
        ],
    )
    waits = []

    class _StopEvent:
        @staticmethod
        def wait(seconds):
            waits.append(seconds)
            return False

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.time.time_ns", lambda: 9)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])
    monkeypatch.setattr(
        "netopsbench.platform.observability.bgp_collector._collect_device_bgp",
        lambda _lab, device, _prefix, timestamp, _topology: [f"{device} {timestamp}"],
    )

    lines = _collect_bgp_lines_paced(metadata_file, interval_seconds=9, parallelism=3, stop_event=_StopEvent())

    assert lines == ["spine1 9", "spine2 9", "leaf1 9"]
    assert waits == [3, 6]


def test_collect_bgp_lines_writes_collection_failure_without_fake_neighbor(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(metadata_file, devices=[Device(name="leaf1", role=DeviceRole.LEAF)])

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "vtysh failed"

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.subprocess.run", lambda *a, **k: _Result())
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])

    lines = collect_bgp_lines(metadata_file, timestamp_ns=9)

    assert len(lines) == 1
    assert lines[0].startswith("bgp_collection,source=leaf1")
    assert "collection_ok=false" in lines[0]
    assert 'error_type="command_failed"' in lines[0]


def test_collect_bgp_lines_reads_native_fat_tree_routing_devices_once(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(
        metadata_file,
        name="ft",
        family="fat-tree",
        devices=[
            Device(name="core1", role=DeviceRole.CORE),
            Device(name="agg1", role=DeviceRole.AGG),
            Device(name="edge1", role=DeviceRole.EDGE),
            Device(name="client1", role=DeviceRole.CLIENT, attached_switch="edge1"),
        ],
    )

    calls = []

    class _Result:
        returncode = 0
        stdout = """
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
"""

    def fake_run(args, capture_output, text, check, timeout):
        calls.append(args)
        return _Result()

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.subprocess.run", fake_run)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])

    collect_bgp_lines(Path(metadata_file), timestamp_ns=7)

    containers = [call[2] for call in calls]
    assert containers == ["clab-ft-core1", "clab-ft-agg1", "clab-ft-edge1"]


def test_run_once_writes_snapshot_and_exits(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    metadata_file.write_text('{"name":"demo","devices":{"spines":[],"leafs":[]}}', encoding="utf-8")
    output_file = tmp_path / "bgp.lp"

    monkeypatch.setattr(
        "netopsbench.platform.observability.bgp_collector.collect_bgp_lines",
        lambda metadata, parallelism=1, topology_id=None: ["bgp_neighbors,source=spine1 value=1i 7"],
    )

    assert run_once(metadata_file, output_file, parallelism=4) == 0
    assert output_file.read_text(encoding="utf-8") == "bgp_neighbors,source=spine1 value=1i 7\n"


def test_write_lines_truncates_existing_bgp_file_when_size_limit_would_be_exceeded(tmp_path):
    output_file = tmp_path / "bgp.lp"
    output_file.write_text("old_snapshot value=1i 1\n" * 4, encoding="utf-8")

    _write_lines(output_file, ["new_snapshot value=2i 2"], max_bytes=32)

    assert output_file.read_text(encoding="utf-8") == "new_snapshot value=2i 2\n"
