"""Fault execution helpers for scenario execution."""

from __future__ import annotations

from netopsbench.logging_utils import get_logger
from netopsbench.platform.faults.specs import get_fault_spec
from netopsbench.platform.utils.events import emit as _emit

logger = get_logger(__name__)


def inject_fault(runner, episode) -> dict:
    """Inject a fault via the centralized fault registry."""

    if episode.fault_type == "none":
        _emit("\n[Fault Injection] No fault - baseline episode")
        return {"success": True, "fault_type": "none", "message": "Baseline episode"}

    _emit(f"\n[Fault Injection] {episode.fault_type} on {episode.target_device}")

    spec = get_fault_spec(episode.fault_type)
    if spec is None or spec.inject_episode is None:
        raise ValueError(f"Unsupported fault type: {episode.fault_type}")

    try:
        result = spec.inject_episode(runner.injector, episode)
    except RuntimeError as exc:
        _emit(f"  ✗ Fault injection failed: {exc}")
        return {"success": False, "fault_type": episode.fault_type, "error": str(exc)}

    if result.get("success"):
        _emit("  ✓ Fault injected successfully")
    else:
        _emit(f"  ✗ Fault injection failed: {result.get('error')}")

    return result


def recover_fault(runner):
    """Recover all active faults."""

    _emit("\n[Recovery] Recovering from faults...")
    results = runner.injector.recover_all()
    _emit(f"  ✓ Recovered from {len(results)} faults")
    return results
