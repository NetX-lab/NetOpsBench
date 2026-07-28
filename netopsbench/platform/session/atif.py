"""ATIF trajectory construction and serialization."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from netopsbench.agents._trace_utils import jsonable as _jsonable
from netopsbench.agents.tracing import AgentTraceRecorder

from .trace_utils import isoformat as _isoformat

ATIF_SCHEMA_VERSION = "ATIF-v1.7"


def build_trace_payload(
    *,
    trace_id: str,
    run_id: str,
    case_id: str,
    scenario_id: str,
    episode_result: dict[str, Any],
    worker: str,
    topology_id: str | None,
    runtime_id: str,
    agent: Any,
    diagnosis: Any,
    diagnosis_payload: dict[str, Any],
    started_at: datetime,
    ended_at: datetime,
    pingmesh_window: dict[str, Any] | None = None,
    error: str | None = None,
    diagnostic_context: Any = None,
    trace_recorder: AgentTraceRecorder | None = None,
    topology_scale: str | None = None,
) -> dict[str, Any]:
    metadata = dict(getattr(diagnosis, "metadata", None) or {})
    model = _agent_model_payload(agent)
    model = {**model, **_model_payload(metadata)}
    if trace_recorder is not None:
        model = {**model, **trace_recorder.model_metadata()}
    steps = _context_steps(diagnostic_context)
    runtime_steps = trace_recorder.to_steps() if trace_recorder is not None else []
    steps.extend(_normalise_steps(runtime_steps, model_metadata=model))
    if not runtime_steps:
        steps.extend(_steps_from_tool_calls(diagnosis_payload.get("tool_calls") or metadata.get("tool_calls") or []))
    steps.append(_final_diagnosis_step(diagnosis_payload, ended_at=ended_at))
    return {
        "trace_id": trace_id,
        "run_id": run_id,
        "case_id": case_id,
        "scenario_id": scenario_id,
        "episode_id": (episode_result.get("episode") or {}).get("episode_id"),
        "worker": worker,
        "topology_id": topology_id,
        "topology_scale": topology_scale,
        "runtime_id": runtime_id,
        "agent": _agent_payload(agent, diagnosis),
        "model": model,
        "pingmesh_window": _jsonable(pingmesh_window or {}),
        "started_at": _isoformat(started_at),
        "ended_at": _isoformat(ended_at),
        "steps": _jsonable(steps),
        "final_diagnosis": _diagnosis_payload(diagnosis_payload),
        "metrics": _metrics_payload(
            metadata,
            diagnosis_payload,
            trace_recorder=trace_recorder,
            started_at=started_at,
            ended_at=ended_at,
        ),
        "error": error or _diagnosis_error(diagnosis, diagnosis_payload),
    }


def build_atif_payload(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": trace.get("run_id"),
        "trajectory_id": trace.get("trace_id"),
        "agent": _atif_agent(trace),
        "steps": _atif_steps(trace),
        "notes": None,
        "final_metrics": _atif_final_metrics(trace),
        "extra": {
            "framework": "netopsbench",
            "case_id": trace.get("case_id"),
            "scenario_id": trace.get("scenario_id"),
            "episode_id": trace.get("episode_id"),
            "runtime_id": trace.get("runtime_id"),
            "worker": trace.get("worker"),
            "pingmesh_window": trace.get("pingmesh_window") or {},
            "topology_id": trace.get("topology_id"),
            "topology_scale": trace.get("topology_scale"),
            "started_at": trace.get("started_at"),
            "ended_at": trace.get("ended_at"),
            "final_diagnosis": trace.get("final_diagnosis") or {},
            "error": trace.get("error"),
        },
    }


def _normalise_steps(steps: list[Any], *, model_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    saw_runtime_event = False
    seen_copied_context: set[str] = set()
    for index, item in enumerate(steps, 1):
        step = dict(item) if isinstance(item, dict) else {"content": str(item)}
        step.setdefault("index", index)
        step["type"] = str(step.get("type") or step.get("role") or step.get("source") or "message")
        if step.get("is_copied_context"):
            fingerprint = _message_fingerprint(step)
            if saw_runtime_event or fingerprint in seen_copied_context:
                continue
            seen_copied_context.add(fingerprint)
            step.pop("is_copied_context", None)
            step["is_initial_context"] = True
        else:
            saw_runtime_event = True
        if model_metadata and step["type"] == "llm":
            step.setdefault("model", model_metadata.get("model"))
            step.setdefault("provider", model_metadata.get("provider"))
        normalised.append(_jsonable(step))
    return normalised


def _atif_steps(trace: dict[str, Any]) -> list[dict[str, Any]]:
    atif_steps: list[dict[str, Any]] = []
    for step_id, native in enumerate(_collapse_copied_context_steps(trace.get("steps") or []), 1):
        step = native if isinstance(native, dict) else {"content": str(native)}
        atif_step: dict[str, Any] = {
            "step_id": step_id,
            "timestamp": step.get("started_at") or step.get("ended_at") or trace.get("started_at"),
            "source": _atif_source(step),
            "message": _atif_message_content(_step_message(step) or ""),
            "extra": _compact_extra(step),
        }
        if atif_step["source"] == "agent" and (step.get("model") or (trace.get("model") or {}).get("model")):
            atif_step["model_name"] = step.get("model") or (trace.get("model") or {}).get("model")
        if step.get("reasoning_content"):
            atif_step["reasoning_content"] = step.get("reasoning_content")
        metrics = _atif_step_metrics(step)
        if metrics:
            atif_step["metrics"] = metrics
        if _is_tool_step(step):
            tool_call_id = str(step.get("tool_call_id") or step.get("run_id") or f"tool-{step_id}")
            function_name = step.get("name") or step.get("tool_name") or step.get("tool") or "tool"
            atif_step["source"] = "agent"
            atif_step["message"] = atif_step["message"] or f"Tool call: {function_name}"
            atif_step["tool_calls"] = [
                {
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "arguments": step.get("args") or step.get("input") or {},
                    "extra": {"run_id": step.get("run_id"), "parent_run_id": step.get("parent_run_id")},
                }
            ]
            observation = step.get("observation")
            if observation is None and step.get("error") is not None:
                observation = {"error": step.get("error")}
            if observation is not None:
                atif_step["observation"] = {
                    "results": [
                        {
                            "source_call_id": tool_call_id,
                            "content": _atif_observation_content(observation),
                            "extra": {"status": "error" if step.get("error") else "success"},
                        }
                    ]
                }
        atif_steps.append(_jsonable(atif_step))
    return atif_steps


def _atif_agent(trace: dict[str, Any]) -> dict[str, Any]:
    agent = trace.get("agent") or {}
    model = trace.get("model") or {}
    return _jsonable(
        {
            "name": agent.get("name") or "unknown",
            "version": agent.get("class") or "unknown",
            "model_name": model.get("model") or "unknown",
            "extra": {"class": agent.get("class"), "provider": model.get("provider"), "runtime": model.get("runtime")},
        }
    )


def _atif_final_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    metrics = trace.get("metrics") or {}
    return _jsonable(
        {
            "total_prompt_tokens": metrics.get("input_tokens"),
            "total_completion_tokens": metrics.get("output_tokens"),
            "total_steps": len(trace.get("steps") or []),
            "extra": {
                "total_tokens": metrics.get("total_tokens"),
                "llm_call_count": metrics.get("llm_call_count"),
                "tool_calls_count": metrics.get("tool_calls_count"),
                "time_taken_seconds": metrics.get("time_taken_seconds"),
            },
        }
    )


def _agent_payload(agent: Any, diagnosis: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(diagnosis, "agent_name", None) or getattr(agent, "name", None) or "unknown"),
        "class": getattr(getattr(agent, "__class__", None), "__name__", type(agent).__name__),
    }


def _agent_model_payload(agent: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    provider = _first_agent_attr(agent, ("vendor", "provider"))
    model = _first_agent_attr(agent, ("model", "model_name"))
    runtime = _first_agent_attr(agent, ("runtime", "runtime_name"))
    if provider:
        payload["provider"] = provider
    if model:
        payload["model"] = model
    if runtime:
        payload["runtime"] = runtime
    return _jsonable(payload)


def _first_agent_attr(agent: Any, names: tuple[str, ...]) -> str | None:
    for candidate in _agent_candidates(agent):
        for name in names:
            value = getattr(candidate, name, None)
            if value is None or callable(value):
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _agent_candidates(agent: Any):
    seen: set[int] = set()
    stack = [agent]
    while stack:
        candidate = stack.pop(0)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        yield candidate
        for attr in ("agent", "_agent", "wrapped_agent", "inner"):
            inner = getattr(candidate, attr, None)
            if inner is not None and id(inner) not in seen:
                stack.append(inner)


def _model_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _jsonable(metadata[key])
        for key in ("provider", "model", "runtime")
        if key in metadata and metadata.get(key) is not None
    }


def _metrics_payload(
    metadata: dict[str, Any],
    diagnosis_payload: dict[str, Any],
    *,
    trace_recorder: AgentTraceRecorder | None = None,
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Any]:
    recorder_metrics = trace_recorder.metrics() if trace_recorder is not None else {}
    payload = {
        "time_taken_seconds": float(
            diagnosis_payload.get("time_taken_seconds") or max(0.0, (ended_at - started_at).total_seconds())
        ),
        "tool_calls_count": len(
            (trace_recorder.tool_calls() if trace_recorder is not None else [])
            or diagnosis_payload.get("tool_calls")
            or metadata.get("tool_calls")
            or []
        ),
    }
    for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
        if recorder_metrics.get(key):
            payload[key] = _jsonable(recorder_metrics[key])
        elif key in metadata:
            payload[key] = _jsonable(metadata[key])
    return payload


def _diagnosis_payload(diagnosis_payload: dict[str, Any]) -> dict[str, Any]:
    final = dict(diagnosis_payload)
    metadata = dict(final.get("metadata") or {})
    metadata.pop("trace", None)
    metadata.pop("trajectory", None)
    final["metadata"] = metadata
    return _jsonable(final)


def _diagnosis_error(diagnosis: Any, diagnosis_payload: dict[str, Any]) -> str | None:
    if diagnosis_payload.get("error"):
        return str(diagnosis_payload["error"])
    if getattr(diagnosis, "success", True) is False:
        findings = getattr(diagnosis, "findings", None) or {}
        if isinstance(findings, dict) and findings.get("error"):
            return str(findings["error"])
    return None


def _context_steps(diagnostic_context: Any) -> list[dict[str, Any]]:
    if diagnostic_context is None:
        return []
    payload = {
        "scenario_id": getattr(diagnostic_context, "scenario_id", None),
        "topology": getattr(diagnostic_context, "topology", None),
        "symptoms": getattr(diagnostic_context, "symptoms", None),
    }
    return [
        {
            "type": "message",
            "source": "user",
            "message": _atif_message_content(_jsonable(payload)),
            "is_copied_context": True,
        }
    ]


def _steps_from_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    return [
        {
            "index": index,
            "type": "tool_call",
            "name": (item if isinstance(item, dict) else {"tool": str(item)}).get("tool")
            or (item if isinstance(item, dict) else {}).get("name")
            or "tool",
            "args": (item if isinstance(item, dict) else {}).get("args")
            or (item if isinstance(item, dict) else {}).get("input")
            or {},
        }
        for index, item in enumerate(tool_calls, 1)
    ]


def _final_diagnosis_step(diagnosis_payload: dict[str, Any], *, ended_at: datetime) -> dict[str, Any]:
    final = _diagnosis_payload(diagnosis_payload)
    verdict = final.get("verdict") or ("error" if final.get("error") else "unknown")
    fault_type = final.get("fault_type")
    location = final.get("location") if isinstance(final.get("location"), dict) else {}
    location_text = ", ".join(str(value) for value in (location or {}).values() if value not in (None, ""))
    parts = [f"Final diagnosis: {verdict}"]
    if fault_type:
        parts.append(f"fault_type={fault_type}")
    if location_text:
        parts.append(f"location={location_text}")
    return {
        "type": "final_diagnosis",
        "source": "agent",
        "message": "; ".join(parts),
        "ended_at": _isoformat(ended_at),
        "extra": {"final": True, "diagnosis": _final_diagnosis_summary(final)},
    }


def _final_diagnosis_summary(final: dict[str, Any]) -> dict[str, Any]:
    return _jsonable(
        {
            key: final[key]
            for key in ("verdict", "fault_type", "location", "evidence", "confidence", "reasoning", "error")
            if key in final and final[key] not in (None, {}, [])
        }
    )


def _atif_source(step: dict[str, Any]) -> str:
    raw = str(step.get("source") or step.get("role") or step.get("type") or "agent").lower()
    if raw == "human":
        return "user"
    if raw in {"system", "user"}:
        return raw
    return "agent"


def _is_tool_step(step: dict[str, Any]) -> bool:
    if str(step.get("type") or "").lower() in {"tool", "tool_call"}:
        return True
    if step.get("tool_call_id"):
        return True
    return bool(step.get("name") and ("args" in step or "input" in step))


def _step_message(step: dict[str, Any]) -> Any:
    for key in ("message", "content", "completion", "prompt"):
        if step.get(key) is not None:
            return step[key]
    return None


def _atif_step_metrics(step: dict[str, Any]) -> dict[str, Any]:
    usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
    metrics: dict[str, Any] = {}
    if usage:
        metrics["prompt_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
        metrics["completion_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
        if usage.get("total_tokens") is not None:
            metrics.setdefault("extra", {})["total_tokens"] = usage["total_tokens"]
    if step.get("duration_seconds") is not None:
        metrics.setdefault("extra", {})["duration_seconds"] = step["duration_seconds"]
    return {key: value for key, value in metrics.items() if value not in (None, {})}


def _atif_observation_content(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)


def _atif_message_content(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)


def _compact_extra(step: dict[str, Any]) -> dict[str, Any]:
    excluded = {
        "message",
        "content",
        "prompt",
        "completion",
        "reasoning_content",
        "tool_calls",
        "observation",
        "args",
        "input",
        "output",
        "usage",
        "extra",
    }
    extra = dict(step.get("extra") or {}) if isinstance(step.get("extra"), dict) else {}
    for key, value in step.items():
        if key not in excluded:
            extra[key] = value
    return extra


def _collapse_copied_context_steps(steps: list[Any]) -> list[Any]:
    collapsed: list[Any] = []
    saw_runtime_event = False
    seen_context: set[str] = set()
    for item in steps:
        if not isinstance(item, dict):
            collapsed.append(item)
            saw_runtime_event = True
            continue
        if item.get("is_copied_context"):
            fingerprint = _message_fingerprint(item)
            if saw_runtime_event or fingerprint in seen_context:
                continue
            copied = dict(item)
            copied.pop("is_copied_context", None)
            copied["is_initial_context"] = True
            collapsed.append(copied)
            seen_context.add(fingerprint)
            continue
        collapsed.append(item)
        saw_runtime_event = True
    return collapsed


def _message_fingerprint(step: dict[str, Any]) -> str:
    return json.dumps(
        _jsonable(
            {
                "type": step.get("type"),
                "source": step.get("source") or step.get("role"),
                "message": step.get("message") if "message" in step else step.get("content"),
                "tool_call_id": step.get("tool_call_id"),
            }
        ),
        sort_keys=True,
        default=str,
    )


__all__ = ["ATIF_SCHEMA_VERSION", "build_atif_payload", "build_trace_payload"]
