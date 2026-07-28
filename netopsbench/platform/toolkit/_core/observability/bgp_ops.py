"""Historical BGP event discovery from centralized telemetry."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..common import ToolResult
from .pingmesh_scope import parse_iso8601_timestamp

_VALID_STATES = {"non_established", "all", "established"}
_EVENT_INDEX_FRESHNESS_TOLERANCE_SECONDS = 60


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso8601_timestamp(str(value), "_time")
    except ValueError:
        return None


def _bool(value: object) -> bool:
    return value is True or str(value).lower() == "true"


class BgpOpsMixin:
    def query_bgp_events(
        self,
        start_time: str | None = None,
        end_time: str | None = None,
        time_range_minutes: int = 10,
        device: str | None = None,
        peer: str | None = None,
        role: str | None = None,
        state: str = "non_established",
        limit: int = 100,
    ) -> ToolResult:
        """Find BGP session transitions without logging in to every router."""
        try:
            if state not in _VALID_STATES:
                raise ValueError(f"Invalid state: {state}. Expected one of {sorted(_VALID_STATES)}")
            safe_device = self._validate_device_name(device) if device else None
            safe_peer = self._validate_ip_address(peer, "peer") if peer else None
            safe_limit = max(1, min(int(limit), 500))
            roles = self._bgp_device_roles()
            if role and role not in set(roles.values()):
                raise ValueError(f"Invalid or unavailable role: {role}")
            if safe_device and safe_device not in roles:
                raise ValueError(f"Unknown routing device: {safe_device}")

            scope = self._resolve_pingmesh_time_scope(time_range_minutes, start_time, end_time)
            filters = self._bgp_flux_filters(safe_device, safe_peer)
            device_filter = self._bgp_flux_filters(safe_device, None)
            selected_devices = {
                name
                for name, device_role in roles.items()
                if (not safe_device or name == safe_device) and (not role or device_role == role)
            }
            role_filter = self._bgp_source_filter(selected_devices) if role and not safe_device else ""
            index_rows = self._query_bgp_event_index(scope, device_filter + role_filter)
            if self._bgp_event_index_covers_scope(index_rows, scope, selected_devices):
                rows = self._query_bgp_event_fast_rows(
                    scope,
                    filters + role_filter,
                    device_filter + role_filter,
                    state,
                )
                events = self._build_bgp_events_fast(
                    rows,
                    scope,
                    roles,
                    safe_device,
                    safe_peer,
                    role,
                    state,
                )
            else:
                rows = self._query_bgp_event_snapshot_rows(
                    scope,
                    filters + role_filter,
                    device_filter + role_filter,
                )
                events = self._build_bgp_events(rows, scope, roles, safe_device, safe_peer, role, state)
            truncated = len(events) > safe_limit
            returned = events[:safe_limit]
            freshness = self._bgp_freshness_seconds(rows + index_rows, scope)
            return ToolResult(
                success=True,
                data={
                    "status": "ok",
                    "time_scope": {
                        key: "episode_context" if key == "source" and value == "toolkit_default" else value
                        for key, value in scope.items()
                        if key != "range_clause"
                    },
                    "events": returned,
                    "returned_events": len(returned),
                    "truncated": truncated,
                    "data_freshness_seconds": freshness,
                },
            )
        except Exception as exc:
            return ToolResult(success=False, data=None, error=str(exc))

    def _query_bgp_event_index(self, scope: dict[str, Any], source_filters: str) -> list[dict[str, Any]]:
        query = f"""
index = from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_event_index")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{source_filters}  |> filter(fn: (r) => r._field == "schema_version")

index
  |> group(columns: ["source"])
  |> first()
  |> yield(name: "index_first")

index
  |> group(columns: ["source"])
  |> last()
  |> yield(name: "index_last")

index
  |> group(columns: ["source"])
  |> count(column: "_value")
  |> yield(name: "index_count")
"""
        return self._query_influx_rows(query, require_value=False)

    def _query_bgp_event_snapshot_rows(
        self,
        scope: dict[str, Any],
        filters: str,
        device_filters: str,
    ) -> list[dict[str, Any]]:
        neighbor_query = f"""
from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{filters}  |> pivot(rowKey: ["_time", "_measurement", "source", "neighbor_address"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""

        collection_query = f"""
from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_collection")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{device_filters}  |> pivot(rowKey: ["_time", "_measurement", "source"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
        window_rows = self._query_influx_rows(neighbor_query, require_value=False)
        window_rows.extend(self._query_influx_rows(collection_query, require_value=False))
        prior_rows: list[dict[str, Any]] = []
        if scope["mode"] == "absolute":
            prior_query = f"""
from(bucket: "{self.influxdb_bucket}")
  |> range(start: -30d, stop: time(v: "{scope["start_time"]}"))
  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{filters}  |> pivot(rowKey: ["_time", "_measurement", "source", "neighbor_address"], columnKey: ["_field"], valueColumn: "_value")
  |> group(columns: ["source", "neighbor_address"])
  |> last(column: "_time")
"""
            prior_rows = self._query_influx_rows(prior_query, require_value=False)
        return [dict(row, _scope_prior=True) for row in prior_rows] + window_rows

    @staticmethod
    def _flux_string(value: object) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _bgp_flux_filters(self, device: str | None, peer: str | None) -> str:
        rendered = ""
        if device:
            rendered += f'  |> filter(fn: (r) => r.source == "{self._flux_string(device)}")\n'
        if peer:
            rendered += f'  |> filter(fn: (r) => r.neighbor_address == "{self._flux_string(peer)}")\n'
        return rendered

    def _bgp_source_filter(self, devices: set[str]) -> str:
        values = ", ".join(f'"{self._flux_string(name)}"' for name in sorted(devices))
        return f"  |> filter(fn: (r) => contains(value: r.source, set: [{values}]))\n"

    @staticmethod
    def _bgp_scope_bounds(scope: dict[str, Any]) -> tuple[datetime, datetime]:
        if scope["mode"] == "absolute":
            return (
                parse_iso8601_timestamp(scope["start_time"], "start_time"),
                parse_iso8601_timestamp(scope["end_time"], "end_time"),
            )
        end = datetime.now(UTC)
        return end - timedelta(minutes=int(scope["time_range_minutes"])), end

    def _bgp_event_index_covers_scope(
        self,
        rows: list[dict[str, Any]],
        scope: dict[str, Any],
        selected_devices: set[str],
    ) -> bool:
        if not rows or not selected_devices:
            return False
        first: dict[str, dict[str, Any]] = {}
        last: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for row in rows:
            source = str(row.get("source") or "")
            result = str(row.get("result") or "")
            if source not in selected_devices:
                continue
            if result == "index_first":
                first[source] = row
            elif result == "index_last":
                last[source] = row
            elif result == "index_count":
                try:
                    counts[source] = int(float(row.get("_value") or 0))
                except (TypeError, ValueError):
                    counts[source] = 0
        if not selected_devices.issubset(first) or not selected_devices.issubset(last):
            return False
        if any(counts.get(source, 0) < 1 for source in selected_devices):
            return False

        start, end = self._bgp_scope_bounds(scope)
        tolerance = timedelta(seconds=_EVENT_INDEX_FRESHNESS_TOLERANCE_SECONDS)
        for source in selected_devices:
            first_time = _timestamp(first[source].get("_time"))
            last_time = _timestamp(last[source].get("_time"))
            try:
                first_schema = int(float(first[source].get("_value") or 0))
                last_schema = int(float(last[source].get("_value") or 0))
            except (TypeError, ValueError):
                return False
            if first_schema < 1 or last_schema < 1 or first_time is None or last_time is None:
                return False
            if first_time > start + tolerance or last_time < end - tolerance:
                return False
        return True

    def _query_bgp_event_fast_rows(
        self,
        scope: dict[str, Any],
        session_filters: str,
        device_filters: str,
        state_filter: str,
    ) -> list[dict[str, Any]]:
        discovery_query = f"""
from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_session_events")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{session_filters}  |> pivot(rowKey: ["_time", "source", "neighbor_address", "event_type"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
  |> yield(name: "session_events")

from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{session_filters}  |> filter(fn: (r) => r._field == "session_state" and r._value != "ESTABLISHED")
  |> yield(name: "non_established_sample")

collections = from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_collection")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{device_filters}  |> filter(fn: (r) => r._field == "collection_ok" or r._field == "error_type")

collections
  |> group(columns: ["source", "_field"])
  |> last()
  |> yield(name: "collection_last")

collections
  |> filter(fn: (r) => r._field == "collection_ok")
  |> group(columns: ["source"])
  |> first()
  |> yield(name: "collection_first")

collections
  |> filter(fn: (r) => r._field == "collection_ok")
  |> group(columns: ["source"])
  |> count(column: "_value")
  |> yield(name: "collection_count")
"""
        discovery_rows = self._query_influx_rows(discovery_query, require_value=False)
        candidates = {
            (str(row.get("source") or ""), str(row.get("neighbor_address") or ""))
            for row in discovery_rows
            if row.get("result") in {"session_events", "non_established_sample"}
            and row.get("source")
            and row.get("neighbor_address")
        }
        if state_filter != "all" and not candidates:
            return discovery_rows
        candidate_filter = "" if state_filter == "all" else self._bgp_session_candidate_filter(candidates)
        detail_filters = session_filters + candidate_filter

        prior_query = ""
        if scope["mode"] == "absolute":
            prior_query = f"""
prior = from(bucket: "{self.influxdb_bucket}")
  |> range(start: -30d, stop: time(v: "{scope["start_time"]}"))
  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{detail_filters}  |> filter(fn: (r) => r._field == "session_state" or r._field == "prefixes_received")

prior
  |> group(columns: ["source", "neighbor_address", "_field"])
  |> last()
  |> yield(name: "prior_field")
"""
        detail_query = f"""
neighbors = from(bucket: "{self.influxdb_bucket}")
{scope["range_clause"]}  |> filter(fn: (r) => r._measurement == "bgp_neighbors")
  |> filter(fn: (r) => r.topology_id == "{self._flux_string(self.topology_id)}")
{detail_filters}  |> filter(fn: (r) => r._field == "session_state" or r._field == "asn" or r._field == "prefixes_received")

neighbors
  |> filter(fn: (r) => r._field == "session_state")
  |> group(columns: ["source", "neighbor_address"])
  |> first()
  |> yield(name: "first_state")

neighbors
  |> filter(fn: (r) => r._field == "session_state")
  |> group(columns: ["source", "neighbor_address"])
  |> last()
  |> yield(name: "last_state")

neighbors
  |> filter(fn: (r) => r._field == "session_state")
  |> group(columns: ["source", "neighbor_address"])
  |> count(column: "_value")
  |> yield(name: "state_count")

neighbors
  |> filter(fn: (r) => r._field == "asn" or r._field == "prefixes_received")
  |> group(columns: ["source", "neighbor_address", "_field"])
  |> last()
  |> yield(name: "latest_field")
{prior_query}
"""
        return discovery_rows + self._query_influx_rows(detail_query, require_value=False)

    def _bgp_session_candidate_filter(self, candidates: set[tuple[str, str]]) -> str:
        predicates = [
            f'(r.source == "{self._flux_string(source)}" and r.neighbor_address == "{self._flux_string(neighbor)}")'
            for source, neighbor in sorted(candidates)
        ]
        return f"  |> filter(fn: (r) => {' or '.join(predicates)})\n"

    def _bgp_device_roles(self) -> dict[str, str]:
        devices = self.topology_metadata.get("devices", []) if self.topology_metadata else []
        if isinstance(devices, dict):
            return {
                str(item["name"]): str(role).removesuffix("s")
                for role, entries in devices.items()
                for item in entries
                if role != "clients" and item.get("name")
            }
        return {
            str(item["name"]): str(item.get("role", "unknown"))
            for item in devices
            if item.get("name") and item.get("role") != "client"
        }

    def _build_bgp_events_fast(
        self,
        rows: list[dict[str, Any]],
        scope: dict[str, Any],
        roles: dict[str, str],
        device: str | None,
        peer: str | None,
        role: str | None,
        state_filter: str,
    ) -> list[dict[str, Any]]:
        sessions: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"transitions": []})
        collections: dict[str, dict[str, Any]] = defaultdict(dict)
        for row in rows:
            source = str(row.get("source") or "")
            if not source or source not in roles or (role and roles[source] != role):
                continue
            result = str(row.get("result") or "")
            neighbor = str(row.get("neighbor_address") or "")
            if result.startswith("collection_"):
                collection = collections[source]
                if result == "collection_first":
                    collection["first_seen"] = row.get("_time")
                elif result == "collection_count":
                    try:
                        collection["sample_count"] = int(float(row.get("_value") or 0))
                    except (TypeError, ValueError):
                        collection["sample_count"] = 0
                elif result == "collection_last":
                    field = str(row.get("_field") or "")
                    collection[field] = row.get("_value")
                    if field == "collection_ok":
                        collection["last_seen"] = row.get("_time")
                continue
            if not neighbor or (device and source != device) or (peer and neighbor != peer):
                continue
            session = sessions[(source, neighbor)]
            if result == "first_state":
                session["first_state"] = str(row.get("_value") or "UNKNOWN").upper()
                session["first_seen"] = row.get("_time")
            elif result == "last_state":
                session["last_state"] = str(row.get("_value") or "UNKNOWN").upper()
                session["last_seen"] = row.get("_time")
            elif result == "state_count":
                try:
                    session["sample_count"] = int(float(row.get("_value") or 0))
                except (TypeError, ValueError):
                    session["sample_count"] = 0
            elif result == "latest_field":
                field = str(row.get("_field") or "")
                session[field] = row.get("_value")
                session[f"{field}_time"] = row.get("_time")
            elif result == "prior_field":
                field = str(row.get("_field") or "")
                session[f"prior_{field}"] = row.get("_value")
                session[f"prior_{field}_time"] = row.get("_time")
            elif result == "session_events":
                session["transitions"].append(row)

        events: list[dict[str, Any]] = []
        for (source, neighbor), session in sessions.items():
            transitions = sorted(
                session["transitions"],
                key=lambda row: _timestamp(row.get("_time")) or datetime.min.replace(tzinfo=UTC),
            )
            first_state = session.get("first_state")
            latest = session.get("last_state")
            last_seen = session.get("last_seen")
            last_seen_dt = _timestamp(last_seen)
            latest_transition_time = _timestamp(transitions[-1].get("_time")) if transitions else None
            if transitions and (
                last_seen_dt is None or (latest_transition_time and latest_transition_time >= last_seen_dt)
            ):
                latest = str(transitions[-1].get("latest_state") or "UNKNOWN").upper()
                last_seen = transitions[-1].get("_time")
            if first_state is None and transitions:
                first_state = str(transitions[0].get("latest_state") or "UNKNOWN").upper()
            if latest is None or first_state is None:
                continue
            previous_value = session.get("prior_session_state")
            previous = str(previous_value).upper() if previous_value is not None else None

            timeline: list[tuple[datetime, str]] = []
            first_seen_dt = _timestamp(session.get("first_seen"))
            if first_seen_dt:
                timeline.append((first_seen_dt, first_state))
            for transition in transitions:
                transition_time = _timestamp(transition.get("_time"))
                if transition_time:
                    timeline.append((transition_time, str(transition.get("latest_state") or "UNKNOWN").upper()))
            snapshot_last_seen_dt = _timestamp(session.get("last_seen"))
            if snapshot_last_seen_dt:
                timeline.append((snapshot_last_seen_dt, str(session.get("last_state") or "UNKNOWN").upper()))
            states = list(dict.fromkeys(state for _, state in sorted(timeline, key=lambda item: item[0]))) or [
                first_state,
                latest,
            ]
            states = list(dict.fromkeys(states))
            saw_down = any(value != "ESTABLISHED" for value in states)

            transition_pairs = [
                (
                    str(item.get("previous_state") or "UNKNOWN").upper(),
                    str(item.get("latest_state") or "UNKNOWN").upper(),
                )
                for item in transitions
            ]
            if previous is not None:
                transition_pairs.insert(0, (previous, first_state))
            down_transition = any(
                before == "ESTABLISHED" and after != "ESTABLISHED" for before, after in transition_pairs
            )
            recovery_transition = any(
                before != "ESTABLISHED" and after == "ESTABLISHED" for before, after in transition_pairs
            )
            if down_transition and recovery_transition:
                event_type = "session_flap"
            elif down_transition:
                event_type = "session_down"
            elif recovery_transition:
                event_type = "session_recovered"
            elif saw_down:
                event_type = "non_established_observed"
            elif state_filter == "all":
                event_type = "established_observed"
            else:
                continue
            if state_filter == "non_established" and not saw_down:
                continue
            if state_filter == "established" and latest != "ESTABLISHED":
                continue

            prefixes_after = session.get("prefixes_received")
            if _timestamp(session.get("prefixes_received_time")) != _timestamp(last_seen):
                prefixes_after = None
            prefixes_before = session.get("prior_prefixes_received")
            if _timestamp(session.get("prior_prefixes_received_time")) != _timestamp(
                session.get("prior_session_state_time")
            ):
                prefixes_before = None
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": neighbor,
                    "peer_as": session.get("asn"),
                    "event_type": event_type,
                    "previous_state": previous,
                    "latest_state": latest,
                    "states_observed": states,
                    "first_seen": session.get("first_seen") or (transitions[0].get("_time") if transitions else None),
                    "last_seen": last_seen,
                    "sample_count": int(session.get("sample_count") or 0),
                    "prefixes_before": prefixes_before,
                    "prefixes_after": prefixes_after,
                }
            )

        for source, collection in collections.items():
            if not _bool(collection.get("collection_ok")):
                events.append(
                    {
                        "device": source,
                        "role": roles[source],
                        "peer": None,
                        "event_type": "collection_gap",
                        "previous_state": None,
                        "latest_state": None,
                        "states_observed": [],
                        "first_seen": collection.get("first_seen"),
                        "last_seen": collection.get("last_seen"),
                        "sample_count": int(collection.get("sample_count") or 0),
                        "error_type": collection.get("error_type"),
                    }
                )
        selected_devices = {
            name
            for name, device_role in roles.items()
            if (not device or name == device) and (not role or device_role == role)
        }
        missing_devices = selected_devices - collections.keys() if not peer or device else set()
        for source in missing_devices:
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": None,
                    "event_type": "collection_gap",
                    "previous_state": None,
                    "latest_state": None,
                    "states_observed": [],
                    "first_seen": None,
                    "last_seen": None,
                    "sample_count": 0,
                    "error_type": "no_collection_samples",
                }
            )
        return sorted(events, key=lambda event: (str(event.get("last_seen") or ""), event["device"]), reverse=True)

    def _build_bgp_events(
        self,
        rows: list[dict[str, Any]],
        scope: dict[str, Any],
        roles: dict[str, str],
        device: str | None,
        peer: str | None,
        role: str | None,
        state_filter: str,
    ) -> list[dict[str, Any]]:
        sessions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        collections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            source = str(row.get("source") or "")
            if not source or source not in roles or (role and roles[source] != role):
                continue
            measurement = row.get("_measurement")
            if measurement == "bgp_collection":
                collections[source].append(row)
            elif measurement == "bgp_neighbors" and row.get("neighbor_address"):
                sessions[(source, str(row["neighbor_address"]))].append(row)

        events: list[dict[str, Any]] = []
        for (source, neighbor), samples in sessions.items():
            if device and source != device or peer and neighbor != peer:
                continue
            samples.sort(key=lambda row: _timestamp(row.get("_time")) or datetime.min.replace(tzinfo=UTC))
            prior = [row for row in samples if row.get("_scope_prior")]
            window = [row for row in samples if not row.get("_scope_prior")]
            if not window:
                continue
            states = [str(row.get("session_state") or "UNKNOWN").upper() for row in window]
            previous = str(prior[-1].get("session_state") or "UNKNOWN").upper() if prior else None
            latest = states[-1]
            saw_down = any(value != "ESTABLISHED" for value in states)
            transition_states = ([previous] if previous else []) + states
            down_transition = any(
                before == "ESTABLISHED" and after != "ESTABLISHED"
                for before, after in zip(transition_states, transition_states[1:], strict=False)
            )
            recovery_transition = any(
                before != "ESTABLISHED" and after == "ESTABLISHED"
                for before, after in zip(transition_states, transition_states[1:], strict=False)
            )
            if down_transition and recovery_transition:
                event_type = "session_flap"
            elif down_transition:
                event_type = "session_down"
            elif recovery_transition:
                event_type = "session_recovered"
            elif saw_down:
                event_type = "non_established_observed"
            elif state_filter == "all":
                event_type = "established_observed"
            else:
                continue
            if state_filter == "non_established" and not saw_down:
                continue
            if state_filter == "established" and latest != "ESTABLISHED":
                continue
            unique_states = list(dict.fromkeys(states))
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": neighbor,
                    "peer_as": window[-1].get("asn"),
                    "event_type": event_type,
                    "previous_state": previous,
                    "latest_state": latest,
                    "states_observed": unique_states,
                    "first_seen": window[0].get("_time"),
                    "last_seen": window[-1].get("_time"),
                    "sample_count": len(window),
                    "prefixes_before": prior[-1].get("prefixes_received") if prior else None,
                    "prefixes_after": window[-1].get("prefixes_received"),
                }
            )

        for source, samples in collections.items():
            latest = samples[-1]
            if not _bool(latest.get("collection_ok")):
                events.append(
                    {
                        "device": source,
                        "role": roles[source],
                        "peer": None,
                        "event_type": "collection_gap",
                        "previous_state": None,
                        "latest_state": None,
                        "states_observed": [],
                        "first_seen": samples[0].get("_time"),
                        "last_seen": latest.get("_time"),
                        "sample_count": len(samples),
                        "error_type": latest.get("error_type"),
                    }
                )
        selected_devices = {
            name
            for name, device_role in roles.items()
            if (not device or name == device) and (not role or device_role == role)
        }
        missing_devices = selected_devices - collections.keys() if not peer or device else set()
        for source in missing_devices:
            events.append(
                {
                    "device": source,
                    "role": roles[source],
                    "peer": None,
                    "event_type": "collection_gap",
                    "previous_state": None,
                    "latest_state": None,
                    "states_observed": [],
                    "first_seen": None,
                    "last_seen": None,
                    "sample_count": 0,
                    "error_type": "no_collection_samples",
                }
            )
        return sorted(events, key=lambda event: (str(event.get("last_seen") or ""), event["device"]), reverse=True)

    @staticmethod
    def _bgp_freshness_seconds(rows: list[dict[str, Any]], scope: dict[str, Any]) -> int | None:
        timestamps = [_timestamp(row.get("_time")) for row in rows]
        latest = max((value for value in timestamps if value), default=None)
        if latest is None:
            return None
        reference = (
            parse_iso8601_timestamp(scope["end_time"], "end_time") if scope["mode"] == "absolute" else datetime.now(UTC)
        )
        return max(0, round((reference - latest).total_seconds()))
