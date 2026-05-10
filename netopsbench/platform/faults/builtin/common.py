"""Shared helpers for builtin fault spec declarations."""

from __future__ import annotations

from typing import Any


def episode_param(episode: Any, key: str, default=None):
    parameters = getattr(episode, "parameters", None) or {}
    if isinstance(parameters, dict) and key in parameters:
        return parameters.get(key, default)
    metadata = getattr(episode, "metadata", None) or {}
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return default


def recover_background_process_fault(injector: Any, fault: dict) -> dict:
    task_id = fault.get("task_id")
    pid = fault.get("pid")
    if task_id not in (None, ""):
        recovered = injector._tracker.stop_background(task_id)
    elif pid not in (None, ""):
        recovered = injector._cmd.terminate_process(pid)
    else:
        recovered = True

    if recovered and task_id not in (None, ""):
        injector._tracker.remove_faults(lambda active: active.get("task_id") == task_id)
    elif recovered and pid not in (None, ""):
        injector._tracker.remove_faults(lambda active: active.get("pid") == pid)

    result = {"type": fault.get("type"), "recovered": recovered}
    if not recovered:
        if task_id not in (None, ""):
            result["error"] = f"failed to stop background task {task_id}"
        elif pid not in (None, ""):
            result["error"] = f"failed to terminate pid {pid}"
    return result
