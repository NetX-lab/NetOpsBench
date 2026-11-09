"""Close fixed observation intervals using existing ordered probe frames.

Boundary rows are evidence only: they never enter the statistical window.
No successful packet delivery is required to prove that a row was ingested.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from netopsbench.platform.utils.control import OperationControl, current_operation_control
from netopsbench.platform.utils.measurements import count_operation

from ._detector_query import _SNAPSHOT_FIELDS, SnapshotQueryResult

_PROGRESS_FIELDS = ("probe_cycle", "destination_batch_index", "port_batch_index")
_PROGRESS_KEYS = (*_PROGRESS_FIELDS, "plan_id", "_time", "src_name", "dst_name")


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Pingmesh timestamps must include a timezone")
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _integer(row, key):
    value = float(row[key])
    if not math.isfinite(value) or not value.is_integer() or value < 0:
        raise ValueError(f"invalid {key}")
    return int(value)


def _key(row):
    return row["src_name"], _integer(row, "probe_cycle"), row["dst_name"], row["_time"]


class DetectorIngestionMixin:
    def _ingestion_control(self, deadline):
        inherited = current_operation_control()
        limits = [value for value in (deadline, inherited.deadline if inherited else None) if value is not None]
        return OperationControl(
            inherited.cancel_event if inherited else None,
            min(limits) if limits else time.monotonic() + 60,
        )

    def _boundary_padding_seconds(self):
        longest = 0
        for client in self._pingmesh_plan.clients:
            run = 0
            for frame in [*client.frames, *client.frames]:
                run = run + 1 if not frame.target_indices else 0
                longest = max(longest, min(run, len(client.frames)))
        return (longest + 2) * self._pingmesh_plan.settings.cycle_interval_seconds

    def _progress_frames(self, rows):
        """Validate frame identities, retaining local failures as real records."""
        frames = defaultdict(dict)
        for row in rows:
            source, cycle, destination, timestamp = _key(row)
            _time(timestamp)
            expected = self._expected_frames.get((source, cycle % self._pingmesh_plan.settings.coverage_epoch_cycles))
            if (
                row.get("plan_id") != self.plan_id
                or expected is None
                or destination not in expected[2]
                or _integer(row, "destination_batch_index") != expected[0]
                or _integer(row, "port_batch_index") != expected[1]
            ):
                raise ValueError("unexpected frame identity, peer or port batch")
            addresses = getattr(self, "_client_addresses", {})
            if any(
                addresses.get(row.get(f"{side}_name")) not in (None, row.get(f"{side}_ip")) for side in ("src", "dst")
            ):
                raise ValueError("client address does not match the manifest")
            previous = frames[source, cycle].get(destination)
            if previous is not None and previous["_time"] != timestamp:
                raise ValueError("repeated cycle/peer at a different timestamp; possible source restart")
            frames[source, cycle][destination] = row
        return frames

    def audit_ingestion(self, progress_rows, snapshot_rows, start_time, end_time):
        """Prove all nonempty frames between two observed boundaries are present."""
        start, end = _time(start_time), _time(end_time)
        pending = []
        expected_rows = set()
        try:
            complete_progress = [
                row for row in progress_rows if all(row.get(key) not in (None, "") for key in _PROGRESS_KEYS)
            ]
            if any(
                not all(row.get(key) not in (None, "") for key in _PROGRESS_KEYS) and start <= _time(row["_time"]) < end
                for row in progress_rows
            ):
                pending.append("progress: missing required fields")
            frames = self._progress_frames(complete_progress)
            by_source = defaultdict(list)
            for (source, cycle), targets in frames.items():
                by_source[source].extend((_time(row["_time"]), cycle) for row in targets.values())
            for source in self._pingmesh_clients:
                ordered = sorted(by_source[source])
                if any(after[1] < before[1] for before, after in zip(ordered, ordered[1:], strict=False)):
                    raise ValueError(f"{source}: cycle/time order conflict; possible source restart or clock reversal")
                before = [cycle for stamp, cycle in ordered if stamp < start]
                after = [cycle for stamp, cycle in ordered if stamp >= end]
                if not before or not after:
                    pending.append(f"{source}: missing {'preceding' if not before else 'following'} boundary")
                    continue
                left, right = max(before), min(after)
                for cycle in range(left, right + 1):
                    expected = self._expected_frames[source, cycle % self._pingmesh_plan.settings.coverage_epoch_cycles]
                    targets = frames.get((source, cycle), {})
                    if set(targets) != expected[2]:
                        pending.append(f"{source}/{cycle}: missing frame targets")
                    expected_rows.update(_key(row) for row in targets.values() if start <= _time(row["_time"]) < end)
            actual = {}
            for row in snapshot_rows:
                # A missing field can be an incompletely flushed pivot row.
                if any(
                    row.get(field) in (None, "")
                    for field in (*_SNAPSHOT_FIELDS, "plan_id", "_time", "src_name", "dst_name")
                ):
                    pending.append("snapshot: missing required fields")
                    continue
                if any(not math.isfinite(float(row[field])) for field in _SNAPSHOT_FIELDS):
                    raise ValueError("snapshot contains a non-finite metric")
                key = _key(row)
                if key in actual and actual[key] != row:
                    raise ValueError("conflicting duplicate snapshot record")
                actual[key] = row
            if set(actual) != expected_rows:
                pending.append("snapshot and frame records do not match")
            # Validate complete snapshot rows independently of progress-query arrival order.
            self._progress_frames(list(actual.values()))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return {"status": "invalid", "error": str(exc)}
        return {
            "status": "pending" if pending else "ready",
            "expected_records": len(expected_rows),
            "observed_records": len(actual),
            "reasons": pending[:20],
            **(
                {
                    "records_sha256": hashlib.sha256(
                        json.dumps(
                            [
                                [*key, *[float(actual[key][field]) for field in _SNAPSHOT_FIELDS]]
                                for key in sorted(actual)
                            ],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                }
                if not pending
                else {}
            ),
        }

    def _query_covered_snapshot(self, start_time, end_time, *, deadline=None):
        control = self._ingestion_control(deadline)
        started = time.monotonic()
        start, end = _time(start_time), _time(end_time)
        if end <= start:
            raise ValueError("Pingmesh window must have positive duration")
        padding = self._boundary_padding_seconds()
        runtime_start = getattr(self, "runtime_start_time", None)
        floor = (
            _time(runtime_start)
            if runtime_start
            else start - timedelta(seconds=self._pingmesh_plan.settings.coverage_epoch_seconds + padding)
        )
        retries = 0
        coverage = {}
        ingestion = {"status": "pending"}
        query_seconds = 0.0
        wait_seconds = 0.0
        request_count = 0
        try:
            while True:
                control.remaining()
                context_start = max(floor, start - timedelta(seconds=padding))
                context_end = min(datetime.now(UTC), end + timedelta(seconds=padding))
                tick = time.monotonic()
                try:
                    context = self._query_snapshot(_iso(context_start), _iso(context_end), deadline=control.deadline)
                    request_count += context.request_count
                finally:
                    query_seconds += time.monotonic() - tick
                control.remaining()
                if context.status != "ok":
                    raise RuntimeError(context.error or "snapshot query failed")
                # One fresh read supplies both frame boundaries and metrics.
                # Context outside [start, end) is never used in anomaly statistics.
                snapshot = SnapshotQueryResult(
                    status="ok",
                    rows=self._slice_rows(context.rows, start_time, end_time),
                    request_count=context.request_count,
                )
                coverage = self.summarize_coverage(snapshot.rows)
                ingestion = self.audit_ingestion(context.rows, snapshot.rows, start_time, end_time)
                control.remaining()
                ingestion["elapsed_seconds"] = time.monotonic() - started
                ingestion["query_seconds"] = query_seconds
                ingestion["wait_seconds"] = wait_seconds
                ingestion["query_requests"] = request_count
                ingestion["verification_seconds"] = max(
                    0.0, ingestion["elapsed_seconds"] - query_seconds - wait_seconds
                )
                coverage.update(ingestion=ingestion, query_retries=retries)
                if ingestion["status"] == "ready":
                    return snapshot, coverage
                if ingestion["status"] == "invalid":
                    raise RuntimeError(ingestion["error"])
                if any("boundary" in reason for reason in ingestion["reasons"]):
                    padding = min(padding * 2, max(padding, (datetime.now(UTC) - floor).total_seconds() + 1))
                tick = time.monotonic()
                try:
                    control.sleep(self._pingmesh_plan.settings.cycle_interval_seconds)
                finally:
                    wait_seconds += time.monotonic() - tick
                retries += 1
                count_operation("ingestion_retries")
        except (TimeoutError, RuntimeError) as exc:
            ingestion = {
                **ingestion,
                "status": "invalid" if ingestion["status"] == "invalid" else "not_ready",
                "error": str(exc),
                "elapsed_seconds": time.monotonic() - started,
                "query_seconds": query_seconds,
                "wait_seconds": wait_seconds,
                "query_requests": request_count,
            }
            coverage.update(ingestion=ingestion, query_retries=retries)
            return SnapshotQueryResult(status="error", rows=[], error=f"Window ingestion: {exc}"), coverage

    def wait_for_history(self, *, deadline):
        """Establish preceding frames before the first fresh baseline starts."""
        control = self._ingestion_control(deadline)
        padding = self._boundary_padding_seconds()
        while True:
            control.remaining()
            now = datetime.now(UTC)
            runtime_start = getattr(self, "runtime_start_time", None)
            floor = _time(runtime_start) if runtime_start else now - timedelta(seconds=padding)
            result = self._query_snapshot(
                _iso(max(floor, now - timedelta(seconds=padding))),
                _iso(now),
                deadline=control.deadline,
                fields=_PROGRESS_FIELDS,
            )
            if result.status != "ok":
                raise RuntimeError(result.error or "Pingmesh history query failed")
            frames = self._progress_frames(
                [row for row in result.rows if all(row.get(key) not in (None, "") for key in _PROGRESS_KEYS)]
            )
            ready = {
                source
                for (source, cycle), targets in frames.items()
                if set(targets)
                == self._expected_frames[source, cycle % self._pingmesh_plan.settings.coverage_epoch_cycles][2]
            }
            if ready == set(self._pingmesh_clients):
                return
            padding = min(padding * 2, max(padding, (now - floor).total_seconds() + 1))
            control.sleep(self._pingmesh_plan.settings.cycle_interval_seconds)
