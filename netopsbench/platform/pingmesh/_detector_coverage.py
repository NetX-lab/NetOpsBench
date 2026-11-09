"""Audit observations against the exact immutable offline probe plan."""

from __future__ import annotations

from collections import Counter


class DetectorCoverageMixin:
    def _compile_coverage_expectations(self) -> None:
        self._expected_combinations = set()
        self._expected_frames = {}
        self._expected_destinations = set()
        for client in self._pingmesh_plan.clients:
            for cycle, frame in enumerate(client.frames):
                names = {client.targets[index] for index in frame.target_indices}
                self._expected_frames[client.name, cycle] = (
                    frame.destination_batch_index,
                    frame.port_batch_index,
                    names,
                )
                self._expected_combinations.update((client.name, name, frame.port_batch_index) for name in names)
                if names:
                    self._expected_destinations.add(frame.destination_batch_index)

    def summarize_coverage(self, rows: list[dict]) -> dict:
        plan = self._pingmesh_plan
        settings = plan.settings
        expected = self._expected_combinations
        valid = []
        invalid_rows = []
        invalid_tasks = set()
        invalid_socket_rows = unexpected_rows = 0
        incomplete_record_rows = 0
        unexpected_records = []
        for row in rows:
            if any(
                row.get(key) in (None, "")
                for key in (
                    "plan_id",
                    "src_name",
                    "dst_name",
                    "probe_cycle",
                    "destination_batch_index",
                    "port_batch_index",
                    "rtt_ports_active",
                    "rtt_ports_total",
                    "local_probe_errors",
                )
            ):
                incomplete_record_rows += 1
            cycle = _field_int(row, "probe_cycle")
            source, destination = row.get("src_name"), row.get("dst_name")
            frame = (
                self._expected_frames.get((source, cycle % settings.coverage_epoch_cycles))
                if cycle is not None and cycle >= 0
                else None
            )
            if (
                row.get("plan_id") != settings.plan_id
                or frame is None
                or _field_int(row, "destination_batch_index") != frame[0]
                or _field_int(row, "port_batch_index") != frame[1]
                or destination not in frame[2]
            ):
                unexpected_rows += 1
                if len(unexpected_records) < 20:
                    unexpected_records.append(
                        {
                            key: row.get(key)
                            for key in (
                                "_time",
                                "src_name",
                                "dst_name",
                                "plan_id",
                                "probe_cycle",
                                "destination_batch_index",
                                "port_batch_index",
                            )
                        }
                    )
                continue
            if not _has_valid_socket_batch(
                row,
                source_ports=settings.source_ports,
                port_batches=settings.port_batches,
            ):
                invalid_socket_rows += 1
                invalid_tasks.add((source, destination, frame[1]))
                invalid_rows.append(
                    {
                        "src": source,
                        "dst": destination,
                        "port_batch_index": frame[1],
                        "probe_cycle": cycle,
                        "time": str(row.get("_time") or row.get("time") or ""),
                        "local_probe_errors": _field_int(row, "local_probe_errors"),
                        "socket_batch_valid": _has_expected_socket_batch(
                            row, source_ports=settings.source_ports, port_batches=settings.port_batches
                        ),
                    }
                )
                continue
            valid.append(row)
        combinations = Counter((row["src_name"], row["dst_name"], int(row["port_batch_index"])) for row in valid)
        pairs = Counter((row["src_name"], row["dst_name"]) for row in valid)
        missing = expected - combinations.keys()
        sources = {row["src_name"] for row in valid}
        cycles_by_source = {}
        for row in valid:
            cycles_by_source.setdefault(row["src_name"], set()).add(int(row["probe_cycle"]))
        cycles = {int(row["probe_cycle"]) for row in valid}
        ports = {int(row["port_batch_index"]) for row in valid}
        destinations = {int(row["destination_batch_index"]) for row in valid}
        complete = not missing and not unexpected_rows and not invalid_socket_rows
        return {
            "status": "ok",
            "plan_id": settings.plan_id,
            "coverage_status": "complete" if complete else "incomplete",
            "expected_epoch_cycles": settings.coverage_epoch_cycles,
            "observed_cycle_span": max(cycles) - min(cycles) + 1 if cycles else 0,
            "min_source_cycle_span": min(
                (max(values) - min(values) + 1 for values in cycles_by_source.values()), default=0
            ),
            "source_clients_observed": len(sources),
            "expected_source_clients": len(plan.clients),
            "destination_pairs_observed": len(pairs),
            "expected_destination_pairs": plan.directed_pair_count,
            "pair_port_combinations_observed": len(combinations),
            "expected_pair_port_combinations": len(expected),
            "missing_pair_port_combinations": len(missing),
            "missing_tasks": sorted(missing),
            "invalid_tasks": sorted(invalid_tasks),
            "invalid_rows": invalid_rows,
            "observable_pairs": sorted(pairs),
            "min_samples_per_pair": min(pairs.values(), default=0),
            "port_batches_observed": sorted(ports),
            "destination_batches_observed": sorted(destinations),
            "missing_port_batches": sorted(set(range(len(settings.port_batches))) - ports),
            "missing_destination_batches": sorted(self._expected_destinations - destinations),
            "missing_source_clients": len(plan.clients) - len(sources),
            "invalid_socket_rows": invalid_socket_rows,
            "unexpected_rows": unexpected_rows,
            "incomplete_record_rows": incomplete_record_rows,
            "unexpected_records": unexpected_records,
        }


def _field_int(row: dict, field: str) -> int | None:
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError):
        return None


def _has_valid_socket_batch(row, *, source_ports, port_batches) -> bool:
    return _has_expected_socket_batch(row, source_ports=source_ports, port_batches=port_batches) and (
        _field_int(row, "local_probe_errors") == 0
    )


def _has_expected_socket_batch(row, *, source_ports, port_batches) -> bool:
    port_batch = _field_int(row, "port_batch_index")
    if port_batch is None or not 0 <= port_batch < len(port_batches):
        return False
    return _field_int(row, "rtt_ports_total") == len(source_ports) and _field_int(row, "rtt_ports_active") == len(
        port_batches[port_batch]
    )


def _socket_batch_key(row: dict) -> tuple[str, str, int] | None:
    source = str(row.get("src_name") or row.get("src_ip") or "")
    destination = str(row.get("dst_name") or row.get("dst_ip") or "")
    if not source or not destination:
        return None
    port_batch_index = _field_int(row, "port_batch_index")
    return source, destination, port_batch_index if port_batch_index is not None else -1


def _regular_loss_by_socket_batch(rows: list[dict]) -> dict[tuple[str, str, int], int]:
    losses: dict[tuple[str, str, int], int] = {}
    for row in rows:
        key = _socket_batch_key(row)
        packets_sent = _field_int(row, "packets_sent")
        packets_lost = _field_int(row, "packets_lost")
        if (
            key is None
            or packets_sent is None
            or packets_sent <= 0
            or packets_lost is None
            or not 0 <= packets_lost <= packets_sent
        ):
            continue
        losses[key] = losses.get(key, 0) + packets_lost
    return losses


def is_explained_df_send_error(
    row: dict,
    *,
    regular_loss_by_socket_batch: dict[tuple[str, str, int], int],
) -> bool:
    """Return true when full-window RTT loss explains a DF-only send error."""
    local_errors = _field_int(row, "local_probe_errors")
    df_mtu_drops = _field_int(row, "df_mtu_drops")
    key = _socket_batch_key(row)
    return bool(
        local_errors
        and local_errors == df_mtu_drops
        and key is not None
        and regular_loss_by_socket_batch.get(key, 0) >= 3
    )
