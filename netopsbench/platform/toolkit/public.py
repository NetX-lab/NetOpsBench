"""Projection of diagnostic results; injection/audit objects are never public."""

from datetime import UTC, datetime

_PRIVATE_KEYS = frozenset(
    {
        "injection",
        "effect_audit",
        "route_evidence",
        "recovery",
        "recovery_journal",
        "fault_type",
        "ground_truth",
        "target_device",
        "target_interface",
        "target_prefix",
        "mechanism",
        "parameters",
        "topology_dir",
        "artifact_path",
        "qdisc",
    }
)
_ERROR_KEYS = frozenset({"error", "address_collection_error", "service_collection_error", "collection_error"})


def public_observation(value):
    """Strip reserved runtime fields recursively, preserving measured symptoms."""
    if isinstance(value, dict):
        projected = {}
        for key, child in value.items():
            if key in _PRIVATE_KEYS or (key.startswith("_") and key not in {"_truncation", "_truncated_text"}):
                continue
            if key in _ERROR_KEYS and child:
                projected[key] = "Observation unavailable"
            elif key == "data_source_status" and child not in ("ok", "unavailable", "error"):
                # Legacy observations append query exceptions to this status.
                projected[key] = "error"
            else:
                projected[key] = public_observation(child)
        return projected
    if isinstance(value, (list, tuple)):
        return [public_observation(child) for child in value]
    return value


def public_failure(failure):
    if failure is None:
        return None
    return failure.model_copy(update={"message": f"{failure.domain.value} failure during {failure.phase}"})


def public_tool_result(result):
    payload = public_observation({"success": result.success, "data": result.data, "error": result.error})
    if isinstance(payload.get("data"), dict):
        payload["data"].setdefault("collected_at", datetime.now(UTC).isoformat())
    return payload
