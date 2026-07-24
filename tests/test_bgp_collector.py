from pathlib import Path

import pytest

from netopsbench.models import topology as topology_models
from netopsbench.models.topology import Collector, Device, DeviceRole, Management, TopologyManifest
from netopsbench.platform.observability.bgp_collector import (
    DEFAULT_BGP_LOG_BACKUP_COUNT,
    DEFAULT_BGP_LOG_MAX_BYTES,
    BgpTransitionTracker,
    _collect_bgp_lines_paced,
    _delete_ingested_segments,
    _query_ingested_segments,
    _sealed_segments,
    _write_lines,
    _write_segmented_lines,
    build_bgp_collection_line,
    build_bgp_lines,
    collect_bgp_lines,
    configure_rotating_log,
    normalize_bgp_state,
    run_loop,
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
    assert all(field not in lines[0] for field in ("msg_rcvd=", "msg_sent=", "in_q=", "out_q=", "up_down="))
    assert lines[0].endswith(" 123456789")


def test_normalize_bgp_state_defaults_to_unknown():
    assert normalize_bgp_state(None) == "UNKNOWN"
    assert normalize_bgp_state("Idle") == "IDLE"


def test_build_bgp_collection_line_records_success_and_failure():
    success = build_bgp_collection_line("leaf1", 7, "runtime-xs", True, "")
    failure = build_bgp_collection_line("leaf1", 8, "runtime-xs", False, "timeout")

    assert success.startswith("bgp_collection,source=leaf1,topology_id=runtime-xs ")
    assert "collection_ok=true" in success
    assert 'error_type="timeout"' in failure
    assert "collection_ok=false" in failure
    assert all(field not in success for field in ("neighbor_count=", "duration_ms="))


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


def test_sparse_bgp_collection_keeps_non_established_neighbors(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(metadata_file)

    class _Result:
        returncode = 0

        def __init__(self, state: str):
            self.stdout = f"""
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            {state}       16 N/A
"""

    def fake_run(args, capture_output, text, check, timeout):
        return _Result("Idle" if args[2].endswith("-leaf1") else "2")

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.subprocess.run", fake_run)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])

    lines = collect_bgp_lines(
        metadata_file,
        timestamp_ns=7,
        topology_id="runtime-xs",
        include_full_snapshot=False,
    )

    neighbors = [line for line in lines if line.startswith("bgp_neighbors,")]
    assert len(neighbors) == 1
    assert "source=leaf1" in neighbors[0]
    assert 'session_state="IDLE"' in neighbors[0]
    assert sum(line.startswith("bgp_collection,") for line in lines) == 2


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
        lambda _lab, device, _prefix, timestamp, _topology, *_args, **_kwargs: [f"{device} {timestamp}"],
    )

    lines = _collect_bgp_lines_paced(metadata_file, interval_seconds=9, parallelism=3, stop_event=_StopEvent())

    assert lines == ["spine1 9", "spine2 9", "leaf1 9"]
    assert waits == [3, 6]


def test_loop_collection_can_stream_completed_device_batches(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    _write_topology(metadata_file)

    class _StopEvent:
        @staticmethod
        def wait(_seconds):
            return False

    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.time.time_ns", lambda: 9)
    monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.docker_prefix", lambda: [])
    monkeypatch.setattr(
        "netopsbench.platform.observability.bgp_collector._collect_device_bgp",
        lambda _lab, device, _prefix, timestamp, _topology, *_args, **_kwargs: [f"{device} {timestamp}"],
    )
    emitted = []

    lines = _collect_bgp_lines_paced(
        metadata_file,
        interval_seconds=2,
        parallelism=2,
        stop_event=_StopEvent(),
        on_lines=emitted.extend,
    )

    assert lines == []
    assert emitted == ["spine1 9", "leaf1 9"]


def test_loop_uses_sparse_snapshots_only_above_large_device_threshold(monkeypatch, tmp_path):
    def collect_modes(device_count: int) -> list[bool]:
        class _StopEvent:
            checks = 0

            def is_set(self):
                self.checks += 1
                return self.checks > 2

            @staticmethod
            def set():
                return None

            @staticmethod
            def wait(_seconds):
                return False

        modes = []
        clock = iter([0.0, 0.0, 10.0, 10.0])
        monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.threading.Event", _StopEvent)
        monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.signal.signal", lambda *_args: None)
        monkeypatch.setattr(
            "netopsbench.platform.observability.bgp_collector._read_topology",
            lambda _path: ("demo", [f"leaf{index}" for index in range(device_count)]),
        )
        monkeypatch.setattr("netopsbench.platform.observability.bgp_collector.time.monotonic", lambda: next(clock))
        monkeypatch.setattr(
            "netopsbench.platform.observability.bgp_collector._collect_bgp_lines_paced",
            lambda *_args, **kwargs: modes.append(kwargs["include_full_snapshot"]) or [],
        )

        run_loop(tmp_path / "topology.json", tmp_path / f"{device_count}.lp", interval_seconds=10)
        return modes

    assert collect_modes(128) == [True, True]
    assert collect_modes(129) == [True, False]


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


def test_write_lines_refuses_to_overwrite_unconsumed_bgp_file(tmp_path):
    output_file = tmp_path / "bgp.lp"
    output_file.write_text("old_snapshot value=1i 1\n" * 4, encoding="utf-8")

    with pytest.raises(BufferError, match="refusing to overwrite"):
        _write_lines(output_file, ["new_snapshot value=2i 2"], max_bytes=32)

    assert output_file.read_text(encoding="utf-8") == "old_snapshot value=1i 1\n" * 4


def test_segmented_spool_rotates_atomically_and_preserves_all_lines(tmp_path):
    output_file = tmp_path / "bgp_neighbors.lp"
    first = "bgp_event_index,source=leaf1 schema_version=1i 100"
    second = "bgp_event_index,source=leaf1 schema_version=1i 200"

    _write_segmented_lines(output_file, [first], max_bytes=1024, segment_bytes=64, topology_id="runtime-xs")
    _write_segmented_lines(output_file, [second], max_bytes=1024, segment_bytes=64, topology_id="runtime-xs")

    segments = _sealed_segments(output_file)
    assert len(segments) == 1
    assert segments[0].read_text(encoding="utf-8") == (
        f"{first}\n"
        "bgp_event_index,source=__spool__,spool_segment=100-0,topology_id=runtime-xs "
        "schema_version=1i 100\n"
    )
    assert output_file.read_text(encoding="utf-8") == f"{second}\n"


def test_segment_marker_is_included_in_bounded_spool_limit(tmp_path):
    output_file = tmp_path / "bgp_neighbors.lp"
    first = "bgp_event_index,source=leaf1 schema_version=1i 100"
    second = "bgp_event_index,source=leaf1 schema_version=1i 200"
    _write_segmented_lines(output_file, [first], max_bytes=130, segment_bytes=64, topology_id="runtime-xs")

    with pytest.raises(BufferError, match="while sealing"):
        _write_segmented_lines(output_file, [second], max_bytes=130, segment_bytes=64, topology_id="runtime-xs")

    assert _sealed_segments(output_file) == []
    assert output_file.read_text(encoding="utf-8") == f"{first}\n"


def test_segment_cleanup_requires_ingested_watermark_and_is_restart_safe(tmp_path):
    output_file = tmp_path / "bgp_neighbors.lp"
    old = tmp_path / "bgp_neighbors.sealed.100-0.lp"
    latest = tmp_path / "bgp_neighbors.sealed.200-0.lp"
    old.write_text("old\n", encoding="utf-8")
    latest.write_text("latest\n", encoding="utf-8")

    assert _delete_ingested_segments(output_file, set()) == []
    assert old.exists() and latest.exists()

    assert _delete_ingested_segments(output_file, {"100-0"}) == [old]
    assert not old.exists()
    assert latest.exists()


def test_segment_cleanup_uses_only_matching_ingested_marker(monkeypatch):
    from netopsbench.platform.observability import bgp_collector
    from netopsbench.platform.observability.influxdb import FluxQueryResult

    captured = {}

    def fake_query(url, token, org, query):
        captured["query"] = query
        return FluxQueryResult(
            status="ok",
            text=(
                "#datatype,string,long,dateTime:RFC3339\n"
                ",result,table,_time,spool_segment\n"
                ",,0,2026-07-24T01:02:03.123456789Z,1784854923123456789-0\n"
            ),
        )

    monkeypatch.setattr(bgp_collector, "query_flux", fake_query)

    ingested = _query_ingested_segments('bucket"name', 'topology"name')

    assert ingested == {"1784854923123456789-0"}
    assert 'from(bucket: "bucket\\"name")' in captured["query"]
    assert 'topology_id == "topology\\"name"' in captured["query"]
    assert "exists r.spool_segment" in captured["query"]


def test_bgp_collector_process_log_keeps_three_files_total(tmp_path):
    import logging
    import sys

    original_stdout, original_stderr = sys.stdout, sys.stderr
    try:
        log_file = tmp_path / "bgp_collector.log"
        configure_rotating_log(log_file, max_bytes=64)
        for index in range(20):
            print(f"collector line {index:02d} with enough bytes")
        sys.stdout.flush()
        handler = logging.getLogger("netopsbench.bgp_collector.process").handlers[0]
        handler.flush()
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    assert DEFAULT_BGP_LOG_MAX_BYTES == 10 * 1024 * 1024
    assert DEFAULT_BGP_LOG_BACKUP_COUNT == 2
    assert log_file.exists()
    assert sorted(path.name for path in tmp_path.glob("bgp_collector.log.*")) == [
        "bgp_collector.log.1",
        "bgp_collector.log.2",
    ]
