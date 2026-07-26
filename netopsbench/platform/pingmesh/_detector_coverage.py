"""Coverage quality audit for bounded Pingmesh destination rotation."""

from __future__ import annotations

from collections import Counter
from math import ceil


class DetectorCoverageMixin:
    """Audit schedule coverage without consulting fault targets."""

    _pingmesh_clients: list[str]
    _pingmesh_policy: dict

    def summarize_coverage(self, rows: list[dict]) -> dict:
        client_count = len(self._pingmesh_clients)
        policy = self._pingmesh_policy
        required_policy_fields = {
            "destination_batch_size",
            "rtt_port_pool_size",
            "rtt_ports_per_cycle",
            "coverage_epoch_cycles",
        }
        missing_policy_fields = sorted(required_policy_fields - set(policy))
        if missing_policy_fields:
            return {
                "status": "error",
                "coverage_status": "error",
                "error": "Pingmesh coverage policy is missing fields: " + ", ".join(missing_policy_fields),
            }
        destination_count = max(0, client_count - 1)
        destination_batch_size = policy["destination_batch_size"] or max(1, destination_count)
        port_pool_size = int(policy["rtt_port_pool_size"])
        ports_per_cycle = int(policy["rtt_ports_per_cycle"])
        expected_destination_batches = max(1, ceil(destination_count / destination_batch_size))
        expected_port_batches = max(1, ceil(port_pool_size / ports_per_cycle))
        expected_cycles = int(policy["coverage_epoch_cycles"])
        if expected_cycles != expected_destination_batches * expected_port_batches:
            return {
                "status": "error",
                "coverage_status": "error",
                "error": "Pingmesh coverage policy is missing or stale; regenerate the topology",
            }

        cycle_rows = [row for row in rows if row.get("probe_cycle") is not None]
        cycles = {int(row["probe_cycle"]) for row in cycle_rows}
        destination_batches = {
            int(row["destination_batch_index"]) for row in rows if row.get("destination_batch_index") is not None
        }
        port_batches = {int(row["port_batch_index"]) for row in rows if row.get("port_batch_index") is not None}
        sources = {row.get("src_name") for row in cycle_rows if row.get("src_name")}
        cycles_by_source: dict[str, set[int]] = {}
        for row in cycle_rows:
            source = row.get("src_name")
            if source:
                cycles_by_source.setdefault(source, set()).add(int(row["probe_cycle"]))
        pair_counts = Counter(
            (row.get("src_name"), row.get("dst_name"))
            for row in cycle_rows
            if row.get("src_name") and row.get("dst_name")
        )
        pair_port_counts = Counter(
            (row.get("src_name"), row.get("dst_name"), int(row["port_batch_index"]))
            for row in cycle_rows
            if row.get("src_name") and row.get("dst_name") and row.get("port_batch_index") is not None
        )
        observed_cycle_span = max(cycles) - min(cycles) + 1 if cycles else 0
        source_cycle_spans = [
            max(source_cycles) - min(source_cycles) + 1 for source_cycles in cycles_by_source.values() if source_cycles
        ]
        min_source_cycle_span = min(source_cycle_spans, default=0)
        missing_destination_batches = sorted(set(range(expected_destination_batches)) - destination_batches)
        missing_port_batches = sorted(set(range(expected_port_batches)) - port_batches)
        missing_sources = max(0, client_count - len(sources))
        invalid_socket_rows = sum(
            1
            for row in cycle_rows
            if not _has_valid_socket_batch(
                row,
                port_pool_size=port_pool_size,
                ports_per_cycle=ports_per_cycle,
                port_batch_count=expected_port_batches,
            )
        )
        expected_pairs = client_count * destination_count
        expected_pair_port_combinations = expected_pairs * expected_port_batches
        complete = bool(
            not missing_destination_batches
            and not missing_port_batches
            and missing_sources == 0
            and len(pair_counts) == expected_pairs
            and len(pair_port_counts) == expected_pair_port_combinations
            and invalid_socket_rows == 0
        )

        return {
            "status": "ok",
            "coverage_status": "complete" if complete else "incomplete",
            "expected_epoch_cycles": expected_cycles,
            "observed_cycle_span": observed_cycle_span,
            "min_source_cycle_span": min_source_cycle_span,
            "source_clients_observed": len(sources),
            "expected_source_clients": client_count,
            "destination_pairs_observed": len(pair_counts),
            "expected_destination_pairs": expected_pairs,
            "pair_port_combinations_observed": len(pair_port_counts),
            "expected_pair_port_combinations": expected_pair_port_combinations,
            "missing_pair_port_combinations": max(
                0,
                expected_pair_port_combinations - len(pair_port_counts),
            ),
            "min_samples_per_pair": min(pair_counts.values(), default=0),
            "port_batches_observed": sorted(port_batches),
            "destination_batches_observed": sorted(destination_batches),
            "missing_port_batches": missing_port_batches,
            "missing_destination_batches": missing_destination_batches,
            "missing_source_clients": missing_sources,
            "invalid_socket_rows": invalid_socket_rows,
        }


def _field_int(row: dict, field: str) -> int | None:
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError):
        return None


def _has_valid_socket_batch(
    row: dict,
    *,
    port_pool_size: int,
    ports_per_cycle: int,
    port_batch_count: int,
) -> bool:
    port_batch_index = _field_int(row, "port_batch_index")
    if port_batch_index is None or not 0 <= port_batch_index < port_batch_count:
        return False
    expected_active = min(ports_per_cycle, port_pool_size - port_batch_index * ports_per_cycle)
    return (
        _field_int(row, "rtt_ports_total") == port_pool_size
        and _field_int(row, "rtt_ports_active") == expected_active
        and (_field_int(row, "local_probe_errors") == 0 or is_complete_loss_df_send_error(row))
    )


def is_complete_loss_df_send_error(row: dict) -> bool:
    """Return true when a DF send error is shadowed by complete RTT path loss."""
    local_errors = _field_int(row, "local_probe_errors")
    df_mtu_drops = _field_int(row, "df_mtu_drops")
    packets_sent = _field_int(row, "packets_sent")
    packets_lost = _field_int(row, "packets_lost")
    return bool(
        local_errors
        and local_errors == df_mtu_drops
        and packets_sent
        and packets_lost is not None
        and packets_lost >= packets_sent
    )
