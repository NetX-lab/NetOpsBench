import inspect
import json
import tempfile
from types import SimpleNamespace

from netopsbench.models.topology import (
    Collector,
    Device,
    DeviceRole,
    Management,
    PingmeshPolicy,
    RoutingMetadata,
    TopologyDefaults,
    TopologyFacts,
    TopologyManifest,
)
from netopsbench.platform.pingmesh._detector_analysis import DetectorAnalysisMixin
from netopsbench.platform.pingmesh._detector_query import SnapshotQueryResult
from netopsbench.platform.pingmesh.detector import Anomaly, AnomalyDetector
from netopsbench.platform.scenario.observation import wait_and_observe
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp import context as mcp_context
from netopsbench.platform.toolkit.mcp import observability as fastmcp_server
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.generator import generate_topology


def _toolkit_with_captured_queries(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        metadata = generate_topology("xs", tmpdir)["metadata"]
    toolkit = AgentToolkit(topology_metadata=metadata)
    captured = {}

    def fake_query(query, require_value=True):
        captured["query"] = query
        captured["require_value"] = require_value
        return []

    monkeypatch.setattr(toolkit, "_query_influx_rows", fake_query)
    return toolkit, captured


def _coverage_detector(client_count=144, *, rtt_ports_per_cycle=4):
    clients = [
        Device(name=f"client{i}", role=DeviceRole.CLIENT, attached_switch="leaf1") for i in range(1, client_count + 1)
    ]
    topology = TopologyManifest(
        topology_id="k12-runtime",
        name="coverage",
        scale="test",
        family="clos",
        management=Management(network="clab-coverage", ipv4_subnet="172.31.1.0/24"),
        collector=Collector(ipv4="172.31.1.200"),
        defaults=TopologyDefaults(),
        facts=TopologyFacts(
            num_leafs=1,
            clients_per_attached_switch=max(1, client_count),
            total_clients=client_count,
            total_switches=1,
        ),
        devices=[Device(name="leaf1", role=DeviceRole.LEAF), *clients],
        links=[],
        routing=RoutingMetadata(ecmp_hash_policy_by_role={DeviceRole.LEAF: 1}),
        pingmesh=PingmeshPolicy(
            destination_batch_size=16,
            rtt_port_pool_size=16,
            rtt_ports_per_cycle=rtt_ports_per_cycle,
            cycle_interval_seconds=2,
        ),
    )
    return AnomalyDetector(
        "http://influxdb:8086",
        "token",
        "org",
        "bucket",
        topology_metadata=topology,
        topology_id="k12-runtime",
    )


def _rtt_rows(values, *, ranges=None, port_batches=None):
    ranges = ranges or [0.2] * len(values)
    rows = []
    for index, value in enumerate(values):
        row = {
            "_time": f"2026-01-01T00:00:{index:02d}Z",
            "_field": "rtt_avg",
            "value": value,
            "rtt_min": value - ranges[index] / 2,
            "rtt_avg": value,
            "rtt_max": value + ranges[index] / 2,
            "src_ip": "192.0.2.1",
            "dst_ip": "192.0.2.2",
            "src_name": "client1",
            "dst_name": "client2",
            "src_leaf": "edge1",
            "dst_leaf": "edge2",
        }
        if port_batches is not None:
            row["port_batch_index"] = port_batches[index]
        rows.append(row)
    return rows


def _snapshot_sequence(monkeypatch, detector, responses):
    pending = iter(responses)
    calls = []

    def query(start_time, end_time):
        calls.append((start_time, end_time))
        return SnapshotQueryResult(status="ok", rows=next(pending))

    monkeypatch.setattr(detector, "_query_snapshot", query)
    return calls


def test_detector_analysis_has_no_case_or_fault_metadata_dependency():
    source = inspect.getsource(DetectorAnalysisMixin)

    for forbidden in ("fault_type", "target_device", "target_interface", "scenario_id", "topology_scale"):
        assert forbidden not in source


def test_rtt_range_is_retained_but_not_promoted_to_an_anomaly():
    detector = _coverage_detector(client_count=2)
    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([10.0, 10.2, 9.9, 10.1], ranges=[0.2, 0.3, 0.2, 0.3]),
        _rtt_rows([10.1, 9.9, 10.2, 10.0], ranges=[0.3, 0.2, 0.3, 0.2]),
    ).anomalies

    assert anomalies == []


def test_latency_detector_reports_sustained_twenty_millisecond_increase():
    detector = _coverage_detector(client_count=2)
    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([1.0, 1.2, 0.9, 1.1, 1.0]),
        _rtt_rows([26.0, 26.2, 25.9, 26.1, 26.0]),
    ).anomalies
    latency = [item for item in anomalies if item.type == "latency_spike"]

    assert len(latency) == 1
    assert latency[0].type == "latency_spike"


def test_latency_detector_ignores_one_high_impact_sample_when_path_median_is_stable():
    detector = _coverage_detector(client_count=2)
    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([1.0, 1.2, 0.9, 1.1]),
        _rtt_rows([1.0, 1.1, 1.0, 101.0]),
    ).anomalies

    assert not [item for item in anomalies if item.type == "latency_spike"]


def test_latency_detector_uses_twenty_millisecond_median_delta_boundary():
    detector = _coverage_detector(client_count=2)

    below = detector.analyze_snapshot_rows(
        _rtt_rows([1.0, 1.0, 1.0, 1.0]),
        _rtt_rows([20.9, 20.9, 20.9, 20.9]),
    ).anomalies
    at_boundary = detector.analyze_snapshot_rows(
        _rtt_rows([1.0, 1.0, 1.0, 1.0]),
        _rtt_rows([21.0, 21.0, 21.0, 21.0]),
    ).anomalies

    assert not [item for item in below if item.type == "latency_spike"]
    latency = [item for item in at_boundary if item.type == "latency_spike"]
    assert len(latency) == 1
    assert latency[0].baseline == 1.0
    assert latency[0].value == 21.0
    assert latency[0].threshold == 21.0
    assert latency[0].timestamp == "2026-01-01T00:00:00Z"


def test_latency_detector_keeps_ecmp_diluted_sustained_path_signal():
    detector = _coverage_detector(client_count=2)

    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([2.0, 2.1, 1.9, 2.0]),
        _rtt_rows([27.0, 27.1, 26.9, 27.0]),
    ).anomalies

    assert [item.type for item in anomalies] == ["latency_spike"]


def test_latency_detector_ignores_one_elevated_ecmp_port_batch():
    detector = _coverage_detector(client_count=2)

    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([1.0] * 4, port_batches=[0, 1, 2, 3]),
        _rtt_rows([26.0, 1.0, 1.0, 1.0], port_batches=[0, 1, 2, 3]),
    ).anomalies

    assert not [item for item in anomalies if item.type == "latency_spike"]


def test_latency_detector_does_not_promote_two_elevated_ecmp_port_batches():
    detector = _coverage_detector(client_count=2)

    anomalies = detector.analyze_snapshot_rows(
        _rtt_rows([1.0] * 4, port_batches=[0, 1, 2, 3]),
        _rtt_rows([26.0, 26.0, 1.0, 1.0], port_batches=[0, 1, 2, 3]),
    ).anomalies
    assert not [item for item in anomalies if item.type == "latency_spike"]


def _probe_sample(
    *,
    timestamp="2026-01-01T00:00:00Z",
    src_ip="192.0.2.1",
    dst_ip="192.0.2.2",
    sent=4,
    lost=0,
    df_sent=4,
    df_lost=0,
):
    return {
        "_time": timestamp,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_name": "client1",
        "dst_name": "client2",
        "src_leaf": "edge1",
        "dst_leaf": "edge2",
        "rtt_avg": 1.0 if lost < sent else 0.0,
        "packets_sent": float(sent),
        "packets_lost": float(lost),
        "packet_loss": (lost / sent) * 100.0,
        "df_packets_sent": float(df_sent),
        "df_packets_lost": float(df_lost),
        "df_mtu_drops": 0.0,
    }


def test_bounded_rotation_missing_paths_are_not_unreachable():
    detector = _coverage_detector(client_count=3)
    baseline = [
        _probe_sample(dst_ip="192.0.2.2"),
        _probe_sample(dst_ip="192.0.2.3"),
    ]
    current = [_probe_sample(dst_ip="192.0.2.2")]

    analysis = detector.analyze_snapshot_rows(baseline, current)

    assert analysis.anomalies == []
    assert analysis.quality["not_observed_paths"] == 1


def test_actual_complete_probe_loss_is_path_unreachable():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=4, df_lost=4)])

    assert [item.type for item in analysis.anomalies] == ["path_unreachable"]
    assert analysis.anomalies[0].samples_sent == 4
    assert analysis.anomalies[0].samples_lost == 4


def test_single_counted_probe_loss_is_not_a_packet_loss_anomaly():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=1)])

    assert not [item for item in analysis.anomalies if item.type == "packet_loss"]


def test_two_counted_probe_losses_are_not_a_packet_loss_anomaly():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=2)])

    assert not [item for item in analysis.anomalies if item.type == "packet_loss"]


def test_distributed_background_loss_does_not_create_a_path_anomaly():
    detector = _coverage_detector(client_count=62)
    baseline = [_probe_sample(dst_ip=f"192.0.2.{index}") for index in range(2, 63)]
    current = [_probe_sample(dst_ip=f"192.0.2.{index}") for index in range(2, 63)]
    current[0] = _probe_sample(dst_ip="192.0.2.2", lost=2)

    analysis = detector.analyze_snapshot_rows(baseline, current)

    assert not [item for item in analysis.anomalies if item.type == "packet_loss"]


def test_endpoint_concentration_does_not_amplify_single_loss_paths():
    detector = _coverage_detector(client_count=11)
    baseline = [_probe_sample(dst_ip=f"192.0.2.{index}") for index in range(2, 12)]
    current = [_probe_sample(dst_ip=f"192.0.2.{index}", lost=1) for index in range(2, 12)]

    analysis = detector.analyze_snapshot_rows(baseline, current)

    assert not [item for item in analysis.anomalies if item.type == "packet_loss"]


def test_df_loss_is_suppressed_when_rtt_is_also_lost():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=4, df_lost=4)])

    assert all(item.type != "mtu_or_fragmentation_suspect" for item in analysis.anomalies)


def test_df_only_loss_is_mtu_suspect():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=0, df_lost=4)])

    assert [item.type for item in analysis.anomalies] == ["mtu_or_fragmentation_suspect"]
    assert analysis.quality["absolute_network_mtu_paths"] == 1


def test_df_loss_with_any_counted_small_probe_loss_is_not_mtu():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(lost=1, df_lost=4)])

    assert not [item for item in analysis.anomalies if item.type == "mtu_or_fragmentation_suspect"]
    assert analysis.quality["absolute_network_mtu_paths"] == 0


def test_df_baseline_losses_are_not_discarded_as_unreachable_samples():
    detector = _coverage_detector(client_count=2)
    baseline = [
        _probe_sample(timestamp=f"2026-01-01T00:00:0{index}Z", df_sent=1, df_lost=int(index < 2)) for index in range(4)
    ]
    current = [
        _probe_sample(timestamp=f"2026-01-01T00:01:0{index}Z", df_sent=1, df_lost=int(index < 3)) for index in range(5)
    ]

    analysis = detector.analyze_snapshot_rows(baseline, current)

    assert not [item for item in analysis.anomalies if item.type == "mtu_or_fragmentation_suspect"]
    assert analysis.quality["absolute_network_mtu_paths"] == 1


def test_absolute_quality_exposes_persistent_loss_hidden_from_delta():
    detector = _coverage_detector(client_count=2)
    persistent = [_probe_sample(sent=4, lost=3)]

    analysis = detector.analyze_snapshot_rows(persistent, persistent)

    assert not [item for item in analysis.anomalies if item.type == "packet_loss"]
    assert analysis.quality["absolute_packet_loss_paths"] == 1


def test_single_counted_df_probe_loss_is_not_an_mtu_suspect():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(df_lost=1)])

    assert not [item for item in analysis.anomalies if item.type == "mtu_or_fragmentation_suspect"]


def test_two_counted_df_probe_losses_are_not_an_mtu_suspect():
    detector = _coverage_detector(client_count=2)

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [_probe_sample(df_lost=2)])

    assert not [item for item in analysis.anomalies if item.type == "mtu_or_fragmentation_suspect"]


def test_local_df_mtu_drop_is_probe_quality_failure_not_network_anomaly():
    detector = _coverage_detector(client_count=2)
    current = _probe_sample(df_lost=1)
    current["df_mtu_drops"] = 1.0

    analysis = detector.analyze_snapshot_rows([_probe_sample()], [current])

    assert analysis.anomalies == []
    assert analysis.quality["local_df_mtu_drops"] == 1


def test_generate_report_queries_each_snapshot_once(monkeypatch):
    detector = _coverage_detector(client_count=2)
    calls = _snapshot_sequence(monkeypatch, detector, [[_probe_sample()], [_probe_sample()]])
    monkeypatch.setattr(
        detector,
        "summarize_coverage",
        lambda _rows: {"status": "ok", "coverage_status": "complete", "expected_epoch_cycles": 4},
    )

    report = detector.generate_windowed_anomaly_report(
        baseline_start="2026-01-01T00:00:00Z",
        baseline_end="2026-01-01T00:01:00Z",
        current_start="2026-01-01T00:01:00Z",
        current_end="2026-01-01T00:01:01Z",
        windows=[],
    )

    assert report["query_status"]["ok"] is True
    assert calls == [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
        ("2026-01-01T00:01:00Z", "2026-01-01T00:01:01Z"),
    ]


def test_generate_report_keeps_baseline_coverage_separate_from_current(monkeypatch):
    detector = _coverage_detector(client_count=2)
    _snapshot_sequence(monkeypatch, detector, [[_probe_sample()], [_probe_sample()]])
    audits = iter(
        [
            {"status": "ok", "coverage_status": "incomplete", "expected_epoch_cycles": 4},
            {"status": "ok", "coverage_status": "complete", "expected_epoch_cycles": 4},
        ]
    )
    monkeypatch.setattr(detector, "summarize_coverage", lambda _rows: next(audits))

    report = detector.generate_windowed_anomaly_report(
        baseline_start="2026-01-01T00:00:00Z",
        baseline_end="2026-01-01T00:00:01Z",
        current_start="2026-01-01T00:01:00Z",
        current_end="2026-01-01T00:01:01Z",
        windows=[],
        include_internal_health=True,
    )

    assert report["_baseline_coverage"]["coverage_status"] == "incomplete"
    assert report["coverage"]["coverage_status"] == "complete"


def test_snapshot_query_failure_is_not_reported_as_healthy(monkeypatch):
    detector = _coverage_detector(client_count=2)
    monkeypatch.setattr(
        detector,
        "_query_snapshot",
        lambda _start, _end: SnapshotQueryResult(status="error", rows=[], error="influx unavailable"),
    )

    report = detector.generate_windowed_anomaly_report(
        baseline_start="baseline-start",
        baseline_end="baseline-end",
        current_start="current-start",
        current_end="current-end",
        windows=[],
    )

    assert report["query_status"] == {"ok": False, "error": "influx unavailable"}
    assert report["coverage"]["coverage_status"] == "error"


def test_snapshot_query_failure_without_error_text_is_still_an_error(monkeypatch):
    detector = _coverage_detector(client_count=2)
    monkeypatch.setattr(
        detector,
        "_query_snapshot",
        lambda _start, _end: SnapshotQueryResult(status="error", rows=[]),
    )

    report = detector.generate_windowed_anomaly_report(
        baseline_start="baseline-start",
        baseline_end="baseline-end",
        current_start="current-start",
        current_end="current-end",
        windows=[],
    )

    assert report["query_status"] == {"ok": False, "error": "query_failed"}


def test_window_slice_handles_fractional_timestamps_at_boundaries():
    rows = [
        {"_time": "2026-01-01T00:00:00.500000000Z", "value": "inside"},
        {"_time": "2026-01-01T00:00:01Z", "value": "end"},
    ]

    selected = AnomalyDetector._slice_rows(
        rows,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )

    assert [row["value"] for row in selected] == ["inside"]


def test_window_merge_keeps_full_window_classification_and_marks_persistence():
    detector = _coverage_detector(client_count=2)
    common = dict(
        src_ip="192.0.2.1",
        src_name="client1",
        dst_ip="192.0.2.2",
        dst_name="client2",
        src_leaf="edge1",
        dst_leaf="edge2",
        baseline=0.0,
        threshold=5.0,
        severity="high",
        timestamp="2026-01-01T00:00:00Z",
    )
    full = Anomaly(type="packet_loss", value=25.0, **common)
    early = Anomaly(type="path_unreachable", value=100.0, **common)
    steady = Anomaly(type="packet_loss", value=50.0, **common)

    merged = detector._merge_window_anomalies([("full", [full]), ("early", [early]), ("steady", [steady])])

    assert len(merged) == 1
    assert merged[0].type == "packet_loss"
    assert merged[0].value == 25.0
    assert merged[0].windows_observed == ["early", "full", "steady"]
    assert merged[0].persistence == "persistent"


def test_window_merge_does_not_promote_short_window_statistical_noise():
    detector = _coverage_detector(client_count=2)
    anomaly = Anomaly(
        type="latency_spike",
        src_ip="192.0.2.1",
        src_name="client1",
        dst_ip="192.0.2.2",
        dst_name="client2",
        src_leaf="edge1",
        dst_leaf="edge2",
        value=8.0,
        baseline=1.0,
        threshold=3.0,
        severity="high",
        timestamp="2026-01-01T00:00:00Z",
    )

    merged = detector._merge_window_anomalies([("full", []), ("early", [anomaly]), ("steady", [])])

    assert merged == []


def test_snapshot_csv_parser_accepts_pivoted_rows():
    csv_text = """#group,false,false,true,true,false,false,false,false
#datatype,string,string,long,dateTime:RFC3339,string,string,double,double
,result,table,_time,src_ip,dst_ip,packets_sent,packets_lost
,_result,0,2026-01-01T00:00:00Z,192.0.2.1,192.0.2.2,4,1
"""

    rows = AnomalyDetector._parse_snapshot_csv(csv_text)

    assert rows == [
        {
            "result": "_result",
            "table": "0",
            "_time": "2026-01-01T00:00:00Z",
            "src_ip": "192.0.2.1",
            "dst_ip": "192.0.2.2",
            "packets_sent": 4.0,
            "packets_lost": 1.0,
        }
    ]


def test_pingmesh_coverage_summary_reports_complete_epoch():
    detector = _coverage_detector()
    rows = []
    for source in range(1, 145):
        for offset in range(1, 144):
            destination_batch = (offset - 1) // 16
            for port_batch in range(4):
                cycle = destination_batch + port_batch * 9
                rows.append(
                    {
                        "probe_cycle": float(cycle),
                        "destination_batch_index": float(destination_batch),
                        "port_batch_index": float(port_batch),
                        "src_name": f"client{source}",
                        "dst_name": f"client{((source + offset - 1) % 144) + 1}",
                        "rtt_ports_active": 4,
                        "rtt_ports_total": 16,
                        "local_probe_errors": 0,
                    }
                )
    audit = detector.summarize_coverage(rows)

    assert audit["coverage_status"] == "complete"
    assert audit["expected_epoch_cycles"] == 36
    assert audit["min_source_cycle_span"] == 36
    assert audit["destination_batches_observed"] == list(range(9))
    assert audit["port_batches_observed"] == list(range(4))
    assert audit["pair_port_combinations_observed"] == 20_592 * 4
    assert audit["missing_pair_port_combinations"] == 0


def test_detector_projects_canonical_manifest_before_loading_coverage_policy(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    generate_topology("xlarge", str(tmp_path), name="coverage-xlarge")
    manifest = load_topology_manifest(tmp_path)

    detector = AnomalyDetector(
        "http://influxdb:8086",
        "token",
        "org",
        "bucket",
        topology_metadata=manifest.model_dump(mode="json"),
        topology_id=manifest.topology_id,
    )

    assert detector._pingmesh_policy["destination_batch_count"] == 8
    assert detector._pingmesh_policy["port_batch_count"] == 4
    assert detector._pingmesh_policy["coverage_epoch_cycles"] == 32


def test_xlarge_and_k8_coverage_counts_all_pairs_and_port_batches():
    client_count = 128
    rows = []
    for source in range(1, client_count + 1):
        for offset in range(1, client_count):
            destination_batch = (offset - 1) // 16
            destination = ((source + offset - 1) % client_count) + 1
            for port_batch in range(4):
                rows.append(
                    {
                        "probe_cycle": float(destination_batch + port_batch * 8),
                        "destination_batch_index": float(destination_batch),
                        "port_batch_index": float(port_batch),
                        "src_name": f"client{source}",
                        "dst_name": f"client{destination}",
                        "rtt_ports_active": 4,
                        "rtt_ports_total": 16,
                        "local_probe_errors": 0,
                    }
                )

    audit = _coverage_detector(client_count).summarize_coverage(rows)

    assert audit["coverage_status"] == "complete"
    assert audit["expected_epoch_cycles"] == 32
    assert audit["destination_pairs_observed"] == 16_256
    assert audit["pair_port_combinations_observed"] == 65_024


def test_coverage_rejects_incomplete_socket_pool():
    detector = _coverage_detector(2)
    rows = [
        {
            "probe_cycle": float(port_batch),
            "destination_batch_index": 0.0,
            "port_batch_index": float(port_batch),
            "src_name": source,
            "dst_name": destination,
            "rtt_ports_active": 4,
            "rtt_ports_total": 0 if source == "client1" and port_batch == 0 else 16,
            "local_probe_errors": 0,
        }
        for source, destination in (("client1", "client2"), ("client2", "client1"))
        for port_batch in range(4)
    ]

    audit = detector.summarize_coverage(rows)

    assert audit["coverage_status"] == "incomplete"
    assert audit["invalid_socket_rows"] == 1


def test_coverage_accepts_smaller_final_port_batch():
    detector = _coverage_detector(2, rtt_ports_per_cycle=6)
    rows = [
        {
            "probe_cycle": float(port_batch),
            "destination_batch_index": 0.0,
            "port_batch_index": float(port_batch),
            "src_name": source,
            "dst_name": destination,
            "rtt_ports_active": 4 if port_batch == 2 else 6,
            "rtt_ports_total": 16,
            "local_probe_errors": 0,
        }
        for source, destination in (("client1", "client2"), ("client2", "client1"))
        for port_batch in range(3)
    ]

    audit = detector.summarize_coverage(rows)

    assert audit["coverage_status"] == "complete"
    assert audit["port_batches_observed"] == [0, 1, 2]
    assert audit["invalid_socket_rows"] == 0


def test_coverage_rejects_local_probe_errors():
    detector = _coverage_detector(2)
    rows = [
        {
            "probe_cycle": float(port_batch),
            "destination_batch_index": 0.0,
            "port_batch_index": float(port_batch),
            "src_name": source,
            "dst_name": destination,
            "rtt_ports_active": 4,
            "rtt_ports_total": 16,
            "local_probe_errors": int(source == "client1" and port_batch == 0),
        }
        for source, destination in (("client1", "client2"), ("client2", "client1"))
        for port_batch in range(4)
    ]

    audit = detector.summarize_coverage(rows)

    assert audit["coverage_status"] == "incomplete"
    assert audit["invalid_socket_rows"] == 1


def test_pingmesh_coverage_summary_reports_missing_batches():
    detector = _coverage_detector()
    rows = [
        {
            "probe_cycle": float(index),
            "destination_batch_index": float(index % 8),
            "port_batch_index": float(index % 3),
            "src_name": "client1",
            "dst_name": "client2",
        }
        for index in range(10)
    ]
    audit = detector.summarize_coverage(rows)

    assert audit["coverage_status"] == "incomplete"
    assert audit["missing_destination_batches"] == [8]
    assert audit["missing_port_batches"] == [3]
    assert audit["missing_source_clients"] == 143


def test_pingmesh_time_scope_uses_explicit_window_first(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)
    toolkit.set_pingmesh_time_window("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z")

    result = toolkit.get_pingmesh_summary(
        time_range_minutes=10,
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-02T00:01:00Z",
    )

    assert result.success is True
    assert result.data["time_scope"]["source"] == "explicit"
    assert 'range(start: time(v: "2026-01-02T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_uses_toolkit_default_before_context_file(monkeypatch, tmp_path):
    context_file = tmp_path / "pingmesh-window.json"
    context_file.write_text(
        json.dumps({"start_time": "2026-01-03T00:00:00Z", "end_time": "2026-01-03T00:01:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", str(context_file))
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)
    toolkit.set_pingmesh_time_window("2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z")

    result = toolkit.get_pingmesh_hotspots()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "toolkit_default"
    assert 'range(start: time(v: "2026-01-02T00:00:00Z")' in captured["query"]


def test_pingmesh_hotspots_applies_global_loss_first_limit(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_hotspots(limit=7)

    assert result.success is True
    query = captured["query"]
    assert '|> pivot(rowKey: ["src_leaf", "dst_leaf"]' in query
    assert '|> group()\n  |> sort(columns: ["packet_loss", "rtt_p99"], desc: true)' in query
    assert "|> limit(n: 7)" in query


def test_pingmesh_summary_queries_only_raw_measurement(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary(
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-02T00:01:00Z",
    )

    assert result.success is True
    assert 'r._measurement == "pingmesh"' in captured["query"]
    assert "aggregateWindow(every: 30s" in captured["query"]
    assert "pingmesh_path_type_30s" not in captured["query"]


def test_pingmesh_hotspots_queries_only_raw_leaf_pair_data(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_hotspots(
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-02T00:01:00Z",
        limit=3,
    )

    assert result.success is True
    assert 'r._measurement == "pingmesh"' in captured["query"]
    assert '|> group(columns: ["src_leaf", "dst_leaf", "_field"])' in captured["query"]
    assert "pingmesh_leaf_pair_30s" not in captured["query"]


def test_pingmesh_time_scope_uses_context_file_before_env(monkeypatch, tmp_path):
    context_file = tmp_path / "pingmesh-window.json"
    context_file.write_text(
        json.dumps({"start_time": "2026-01-03T00:00:00Z", "end_time": "2026-01-03T00:01:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", str(context_file))
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_START_TIME", "2026-01-04T00:00:00Z")
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_END_TIME", "2026-01-04T00:01:00Z")
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "context_file"
    assert 'range(start: time(v: "2026-01-03T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_uses_env_before_rolling(monkeypatch):
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_START_TIME", "2026-01-04T00:00:00Z")
    monkeypatch.setenv("NETOPSBENCH_PINGMESH_END_TIME", "2026-01-04T00:01:00Z")
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary()

    assert result.success is True
    assert result.data["time_scope"]["source"] == "env"
    assert 'range(start: time(v: "2026-01-04T00:00:00Z")' in captured["query"]


def test_pingmesh_time_scope_falls_back_to_rolling(monkeypatch):
    toolkit, captured = _toolkit_with_captured_queries(monkeypatch)

    result = toolkit.get_pingmesh_summary(time_range_minutes=7)

    assert result.success is True
    assert result.data["time_scope"] == {
        "mode": "rolling",
        "source": "time_range_minutes",
        "time_range_minutes": 7,
    }
    assert "|> range(start: -7m)" in captured["query"]


def test_fastmcp_pingmesh_tools_pass_absolute_window(monkeypatch):
    calls = {}

    class FakeToolkit:
        def get_pingmesh_summary(self, **kwargs):
            calls["summary"] = kwargs
            return ToolResult(success=True, data={"ok": "summary"})

        def get_pingmesh_hotspots(self, **kwargs):
            calls["hotspots"] = kwargs
            return ToolResult(success=True, data={"ok": "hotspots"})

    monkeypatch.setattr(mcp_context, "_toolkit", FakeToolkit())

    assert fastmcp_server.get_pingmesh_summary(
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:01:00Z",
    ) == {"ok": "summary"}
    assert fastmcp_server.get_pingmesh_hotspots(
        limit=3,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:01:00Z",
    ) == {"ok": "hotspots"}
    assert calls["summary"]["start_time"] == "2026-01-01T00:00:00Z"
    assert calls["summary"]["end_time"] == "2026-01-01T00:01:00Z"
    assert calls["hotspots"]["limit"] == 3
    assert calls["hotspots"]["start_time"] == "2026-01-01T00:00:00Z"


def test_builtin_mcp_config_passes_netopsbench_env(monkeypatch):
    from netopsbench.sdk.mcp import builtin_mcp_server_config

    monkeypatch.setenv("NETOPSBENCH_PINGMESH_CONTEXT_FILE", "/tmp/window.json")
    config = builtin_mcp_server_config()

    assert config["netopsbench"]["env"]["NETOPSBENCH_PINGMESH_CONTEXT_FILE"] == "/tmp/window.json"


def test_pingmesh_aggregation_keeps_endpoint_counts_without_transit_attribution():
    detector = _coverage_detector(client_count=2)
    anomaly = Anomaly(
        type="packet_loss",
        src_ip="192.0.2.1",
        src_name="client1",
        dst_ip="192.0.2.2",
        dst_name="client2",
        src_leaf="leaf1",
        dst_leaf="leaf2",
        value=25.0,
        baseline=0.0,
        threshold=5.0,
        severity="high",
        timestamp="2026-01-01T00:00:00Z",
    )

    aggregated = detector._aggregate_anomalies([anomaly])

    assert set(aggregated) == {"by_src_leaf", "by_dst_leaf"}
    assert aggregated["by_src_leaf"]["leaf1"]["drop_count"] == 1
    assert aggregated["by_dst_leaf"]["leaf2"]["drop_count"] == 1


def test_scenario_observation_passes_current_topology_to_pingmesh_detector(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        topology = generate_topology("fat-tree-k8", tmpdir)["metadata"]
    captured = {}

    class FakeDetector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate_windowed_anomaly_report(self, **_kwargs):
            return {
                "summary": {"total_anomalies": 0},
                "query_status": {"ok": True, "error": None},
                "coverage": {"status": "ok", "coverage_status": "complete"},
                "anomalies": [],
            }

    monkeypatch.setattr("netopsbench.platform.pingmesh.detector.AnomalyDetector", FakeDetector)
    runner = SimpleNamespace(
        influxdb_url="http://influxdb:8086",
        influxdb_token="token",
        influxdb_org="org",
        influxdb_bucket="bucket",
        topology_id="fat-tree-runtime",
        topology_metadata=topology,
    )

    observations = wait_and_observe(
        runner,
        duration=0,
        baseline_window={"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"},
    )

    assert observations["data_source_status"] == "ok"
    assert captured["topology_metadata"] == topology
