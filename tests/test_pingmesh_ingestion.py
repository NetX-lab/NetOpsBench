"""Closed-window readiness must include repeated tasks at the window tail."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from netopsbench.platform.pingmesh._detector_query import _SNAPSHOT_FIELDS, SnapshotQueryResult
from netopsbench.platform.pingmesh.detector import AnomalyDetector
from netopsbench.platform.topology.generator import generate_topology

START = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
END = START + timedelta(seconds=4)


def rows_for(detector, cycles=range(7)):
    rows = []
    for source in detector._pingmesh_clients:
        for cycle in cycles:
            destination_batch, port_batch, targets = detector._expected_frames[
                source, cycle % detector._pingmesh_plan.settings.coverage_epoch_cycles
            ]
            for target in sorted(targets):
                row = dict.fromkeys(_SNAPSHOT_FIELDS, 0)
                row.update(
                    _time=(START + timedelta(seconds=cycle - 1)).isoformat(),
                    plan_id=detector.plan_id,
                    src_name=source,
                    dst_name=target,
                    src_ip=detector._client_addresses[source],
                    dst_ip=detector._client_addresses[target],
                    probe_cycle=cycle,
                    destination_batch_index=destination_batch,
                    port_batch_index=port_batch,
                    rtt_ports_active=len(detector._pingmesh_plan.settings.port_batches[port_batch]),
                    rtt_ports_total=16,
                    packets_sent=4,
                    df_packets_sent=1,
                )
                rows.append(row)
    return rows


@pytest.fixture
def detector(tmp_path):
    manifest = generate_topology("xs", str(tmp_path))["metadata"]
    return AnomalyDetector("http://test", "token", "org", "bucket", topology_metadata=manifest)


def audit(detector, progress, snapshot=None):
    if snapshot is None:
        snapshot = detector._slice_rows(progress, START.isoformat(), END.isoformat())
    return detector.audit_ingestion(progress, snapshot, START.isoformat(), END.isoformat())


def test_coverage_complete_does_not_prove_tail_arrival(detector):
    progress = rows_for(detector)
    current = detector._slice_rows(progress, START.isoformat(), END.isoformat())
    partial = [r for r in current if not (r["src_name"] == "client1" and r["probe_cycle"] == 4)]
    assert detector.summarize_coverage(partial)["coverage_status"] == "complete"
    assert audit(detector, progress, partial)["status"] == "pending"
    assert audit(detector, progress, current)["status"] == "ready"


@pytest.mark.parametrize("damage", ["whole_round", "field", "progress_field", "following", "preceding"])
def test_missing_data_or_boundary_cannot_close_window(detector, damage):
    progress = rows_for(detector)
    current = detector._slice_rows(progress, START.isoformat(), END.isoformat())
    if damage == "whole_round":
        progress = [r for r in progress if r["probe_cycle"] != 3]
        current = [r for r in current if r["probe_cycle"] != 3]
    elif damage == "field":
        current[0] = {k: v for k, v in current[0].items() if k != "df_packets_lost"}
    elif damage == "progress_field":
        progress[1] = {k: v for k, v in progress[1].items() if k != "port_batch_index"}
    elif damage == "following":
        progress = [r for r in progress if r["probe_cycle"] < 5]
    else:
        progress = [r for r in progress if r["probe_cycle"] > 0]
    assert audit(detector, progress, current)["status"] == "pending"


@pytest.mark.parametrize("damage", ["restart", "clock", "peer", "port", "address"])
def test_conflicting_identity_is_invalid(detector, damage):
    progress = rows_for(detector)
    row = progress[3]
    if damage == "restart":
        progress.append({**row, "_time": (START + timedelta(seconds=2.5)).isoformat()})
    elif damage == "clock":
        row["_time"] = (START - timedelta(seconds=2)).isoformat()
    elif damage == "peer":
        row["dst_name"] = "wrong"
    elif damage == "port":
        row["port_batch_index"] = 9
    else:
        row["src_ip"] = "192.0.2.99"
    assert audit(detector, progress)["status"] == "invalid"


def test_loss_and_local_errors_are_ingested_but_remain_invalid_coverage(detector):
    progress = rows_for(detector)
    for row in progress:
        row.update(packets_lost=4, df_packets_lost=1, local_probe_errors=1)
    current = detector._slice_rows(progress, START.isoformat(), END.isoformat())
    assert audit(detector, progress, current)["status"] == "ready"
    assert detector.summarize_coverage(current)["coverage_status"] == "incomplete"


def test_out_of_order_identical_retries_are_idempotent(detector):
    progress = rows_for(detector)
    assert audit(detector, [*reversed(progress), deepcopy(progress[3])])["status"] == "ready"


def test_empty_frames_are_inferred_from_plan(detector):
    for frame in detector._pingmesh_plan.clients[0].frames[1::2]:
        frame.target_indices.clear()
    detector._compile_coverage_expectations()
    progress = rows_for(detector)
    assert audit(detector, progress)["status"] == "ready"
    assert detector._boundary_padding_seconds() == 3


def test_query_waits_for_late_tail_and_keeps_query_interval(detector, monkeypatch):
    progress = rows_for(detector)
    current = detector._slice_rows(progress, START.isoformat(), END.isoformat())
    clock = [0.0]
    from netopsbench.platform.pingmesh import _detector_ingestion as module

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.OperationControl, "sleep", lambda self, seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    calls = []

    def query(start, end, *, deadline, fields=None):
        calls.append((start, end, deadline, fields))
        rows = progress
        if clock[0] == 0:
            rows = [r for r in rows if not (r["src_name"] == "client1" and r["probe_cycle"] == 4)]
        return SnapshotQueryResult(status="ok", rows=rows, request_count=1)

    monkeypatch.setattr(detector, "_query_snapshot", query)
    result, coverage = detector._query_covered_snapshot(START.isoformat(), END.isoformat(), deadline=5)
    assert result.status == "ok"
    assert coverage["ingestion"]["status"] == "ready"
    assert coverage["query_retries"] == 1
    assert result.rows == current
    assert all(datetime.fromisoformat(a) < START and datetime.fromisoformat(b) > END for a, b, _, _ in calls)
    assert all(fields is None for _, _, _, fields in calls)
    assert len(calls) == coverage["ingestion"]["query_requests"] == 2
    assert {limit for _, _, limit, _ in calls} == {5}


def test_missing_rows_exhaust_one_shared_deadline(detector, monkeypatch):
    from netopsbench.platform.pingmesh import _detector_ingestion as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.OperationControl, "sleep", lambda self, seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    calls = []
    monkeypatch.setattr(detector, "_query_snapshot", lambda *a, **kw: calls.append(kw) or SnapshotQueryResult("ok", []))
    snapshot, coverage = detector._query_covered_snapshot(START.isoformat(), END.isoformat(), deadline=2)
    assert snapshot.status == "error"
    assert coverage["ingestion"]["status"] == "not_ready"
    assert len(calls) == 2


def test_failed_query_time_is_still_reported(detector, monkeypatch):
    from netopsbench.platform.pingmesh import _detector_ingestion as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def fail(*args, **kwargs):
        clock[0] += 0.25
        return SnapshotQueryResult("error", [], "connection failed")

    monkeypatch.setattr(detector, "_query_snapshot", fail)
    snapshot, coverage = detector._query_covered_snapshot(START.isoformat(), END.isoformat(), deadline=2)
    assert snapshot.status == "error"
    assert coverage["ingestion"]["query_seconds"] == 0.25
    assert coverage["ingestion"]["wait_seconds"] == 0


def test_combined_read_keeps_boundary_metrics_outside_statistics(detector, monkeypatch):
    context = rows_for(detector)
    expected = detector._slice_rows(context, START.isoformat(), END.isoformat())
    for row in context:
        if not START <= datetime.fromisoformat(row["_time"]) < END:
            row.update(packets_lost=4, df_packets_lost=1, rtt_avg=100000)
    reads = []

    def query(start, end, **kwargs):
        reads.append((start, end, kwargs))
        return SnapshotQueryResult("ok", context, request_count=1)

    monkeypatch.setattr(detector, "_query_snapshot", query)
    result, coverage = detector._query_covered_snapshot(START.isoformat(), END.isoformat())
    assert result.rows == expected
    assert coverage["ingestion"]["status"] == "ready"
    assert detector.analyze_snapshot_rows(result.rows, result.rows).anomalies == []
    assert len(reads) == coverage["ingestion"]["query_requests"] == 1


def test_completed_audit_after_deadline_cannot_return_ready(detector, monkeypatch):
    from netopsbench.platform.pingmesh import _detector_ingestion as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        detector, "_query_snapshot", lambda *a, **kw: SnapshotQueryResult("ok", rows_for(detector), request_count=1)
    )
    audit = detector.audit_ingestion

    def slow_audit(*args):
        result = audit(*args)
        assert result["status"] == "ready"
        clock[0] = 6
        return result

    monkeypatch.setattr(detector, "audit_ingestion", slow_audit)
    result, coverage = detector._query_covered_snapshot(START.isoformat(), END.isoformat(), deadline=5)
    assert result.status == "error"
    assert coverage["ingestion"]["status"] == "not_ready"
    assert coverage["ingestion"]["query_requests"] == 1


def test_boundary_round_can_straddle_window(tmp_path):
    manifest = generate_topology("medium", str(tmp_path))["metadata"]
    detector = AnomalyDetector("http://test", "token", "org", "bucket", topology_metadata=manifest)
    progress = rows_for(detector)
    row = next(r for r in progress if r["src_name"] == "client1" and r["probe_cycle"] == 0)
    row["_time"] = (START + timedelta(microseconds=1)).isoformat()
    for following in progress:
        if following["src_name"] == "client1" and following["probe_cycle"] == 1:
            following["_time"] = (START + timedelta(milliseconds=1)).isoformat()
    assert audit(detector, progress)["status"] == "ready"
    partial = [r for r in progress if r is not row]
    assert audit(detector, partial)["status"] == "pending"


def test_later_partial_frame_does_not_block_a_closed_window(detector):
    progress = rows_for(detector)
    partial = deepcopy(progress[-1])
    partial.pop("port_batch_index")
    assert audit(detector, [*progress, partial])["status"] == "ready"


def test_archived_k8_four_to_five_samples_cannot_pass_early(tmp_path):
    trace = json.loads((Path(__file__).parent / "fixtures" / "pingmesh_late_tail.json").read_text())
    manifest = generate_topology("fat-tree-k8", str(tmp_path))["metadata"]
    detector = AnomalyDetector("http://test", "token", "org", "bucket", topology_metadata=manifest)
    detector._pingmesh_clients = ["client31"]
    progress = rows_for(detector, range(1068, 1084))
    origin = datetime.fromisoformat(trace["current"][0]["_time"].replace("Z", "+00:00"))
    original = {int(row["probe_cycle"]): row for row in trace["current"]}
    for row in progress:
        row["_time"] = (origin + timedelta(seconds=3 * (row["probe_cycle"] - 1069))).isoformat()
        if row["dst_name"] == "client115":
            row.update(original[row["probe_cycle"]])
            row["plan_id"] = detector.plan_id  # Bind the archived trace to this generated test topology.
    start, end = trace["provenance"]["start"], trace["provenance"]["end"]
    current = detector._slice_rows(progress, start, end)
    partial = [r for r in current if not (r["dst_name"] == "client115" and r["probe_cycle"] == 1081)]
    first_audit = detector.audit_ingestion(progress, partial, start, end)
    assert first_audit["status"] == "pending", first_audit
    assert detector.audit_ingestion(progress, current, start, end)["status"] == "ready"
    early = detector.analyze_snapshot_rows(trace["baseline"], trace["current"][:-1]).anomalies
    final = detector.analyze_snapshot_rows(trace["baseline"], trace["current"]).anomalies
    assert next(a.value for a in early if a.type == "packet_loss") == 75
    assert next(a.value for a in final if a.type == "packet_loss") == 80
    assert any(a.type == "mtu_or_fragmentation_suspect" for a in early)
    assert not any(a.type == "mtu_or_fragmentation_suspect" for a in final)
