"""Result parsing and serialization helpers for the minimal_deepagent example."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from netopsbench.sdk.agents import DiagnosisResult

from ..schema import DiagnosisOutput


def _message_attr(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _token_usage_from_message(message: Any) -> Dict[str, int]:
    usage = _message_attr(message, "usage_metadata") or {}
    response_metadata = _message_attr(message, "response_metadata") or {}
    token_usage = (response_metadata.get("token_usage") or {}) if isinstance(response_metadata, dict) else {}

    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = token_usage.get("prompt_tokens", 0)

    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = token_usage.get("completion_tokens", 0)

    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = token_usage.get("total_tokens")

    try:
        input_tokens = int(input_tokens or 0)
    except (TypeError, ValueError):
        input_tokens = 0

    try:
        output_tokens = int(output_tokens or 0)
    except (TypeError, ValueError):
        output_tokens = 0

    try:
        total_tokens = int(total_tokens) if total_tokens is not None else input_tokens + output_tokens
    except (TypeError, ValueError):
        total_tokens = input_tokens + output_tokens

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "has_usage": int(any((input_tokens, output_tokens, total_tokens))),
    }


def _collect_token_counts(messages: List[Any]) -> Dict[str, int]:
    counts = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "llm_call_count": 0,
    }
    for message in messages:
        usage = _token_usage_from_message(message)
        counts["input_tokens"] += usage["input_tokens"]
        counts["output_tokens"] += usage["output_tokens"]
        counts["total_tokens"] += usage["total_tokens"]
        counts["llm_call_count"] += usage["has_usage"]
    return counts


def _parse_raw_result(raw: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, int]]:
    """Normalise DeepAgent output into ``(structured_dict, tool_calls, token_counts)``."""
    payload = raw if isinstance(raw, dict) else {}
    messages = payload.get("messages", [])

    structured = payload.get("structured_response")
    if structured is not None and hasattr(structured, "model_dump"):
        structured = structured.model_dump()
    elif not isinstance(structured, dict):
        structured = {}

    schema_name = DiagnosisOutput.__name__
    tool_calls = [
        {"tool": getattr(msg, "name", None) or "mcp_tool", "args": {}}
        for msg in messages
        if getattr(msg, "type", None) == "tool"
        and getattr(msg, "name", None) != schema_name
    ]
    return structured, tool_calls, _collect_token_counts(messages)


def _build_diagnosis_result(
    agent_name: str,
    vendor: str,
    model: str,
    structured: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
    token_counts: Dict[str, int] | None = None,
) -> DiagnosisResult:
    """Build a successful ``DiagnosisResult`` from parsed structured output."""
    location = structured.get("location") or {}
    evidence = structured.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]

    confidence = structured.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    return DiagnosisResult(
        agent_name=agent_name,
        verdict=structured.get("verdict", "inconclusive"),
        success=True,
        findings={
            "fault_type": structured.get("fault_type"),
            "location": {"device": location.get("device"), "interface": location.get("interface")},
            "evidence": evidence,
        },
        confidence=confidence,
        reasoning=structured.get("reasoning", ""),
        metadata={
            "provider": vendor,
            "model": model,
            "runtime": "deepagents+mcp",
            "tool_calls": tool_calls,
            "input_tokens": int((token_counts or {}).get("input_tokens", 0) or 0),
            "output_tokens": int((token_counts or {}).get("output_tokens", 0) or 0),
            "total_tokens": int((token_counts or {}).get("total_tokens", 0) or 0),
            "llm_call_count": int((token_counts or {}).get("llm_call_count", 0) or 0),
        },
    )


def _error_result(
    agent_name: str,
    vendor: str,
    model: str,
    exc: BaseException,
    tool_calls: List[Dict[str, Any]] | None = None,
    token_counts: Dict[str, int] | None = None,
) -> DiagnosisResult:
    """Build an ``inconclusive`` fallback result on failure."""
    return DiagnosisResult(
        agent_name=agent_name,
        verdict="inconclusive",
        success=False,
        findings={"fault_type": None, "location": {}, "evidence": [], "error": str(exc)},
        confidence=0.0,
        reasoning=str(exc),
        metadata={
            "provider": vendor,
            "model": model,
            "runtime": "deepagents+mcp",
            "tool_calls": tool_calls or [],
            "input_tokens": int((token_counts or {}).get("input_tokens", 0) or 0),
            "output_tokens": int((token_counts or {}).get("output_tokens", 0) or 0),
            "total_tokens": int((token_counts or {}).get("total_tokens", 0) or 0),
            "llm_call_count": int((token_counts or {}).get("llm_call_count", 0) or 0),
            "error_type": type(exc).__name__,
            "agent_failure_stage": "diagnose",
        },
    )
