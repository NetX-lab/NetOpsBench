"""Result parsing and serialization helpers for the minimal_deepagent example."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from examples.agents.diagnosis import build_result
from netopsbench.sdk.agents import DiagnosisResult

from ..schema import DiagnosisOutput

_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", re.DOTALL)


def _message_attr(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _token_usage_from_message(message: Any) -> dict[str, int]:
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


def _collect_token_counts(messages: list[Any]) -> dict[str, int]:
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


def _parse_raw_result(raw: Any) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Normalise DeepAgent output into ``(structured_dict, tool_calls, token_counts)``."""
    payload = raw if isinstance(raw, dict) else {}
    messages = payload.get("messages", [])

    structured = _structured_from_final_message(messages)

    schema_name = DiagnosisOutput.__name__
    tool_calls = [
        {"tool": getattr(msg, "name", None) or "mcp_tool", "args": {}}
        for msg in messages
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) != schema_name
    ]
    return structured, tool_calls, _collect_token_counts(messages)


def _structured_from_final_message(messages: list[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        if _message_attr(message, "type") not in {"ai", "assistant"}:
            continue
        content = _message_attr(message, "content")
        if not isinstance(content, str) or not content.strip():
            continue
        parsed = _parse_diagnosis_json(content)
        if parsed:
            return parsed
    return {}


def _parse_diagnosis_json(text: str) -> dict[str, Any]:
    for candidate in _json_candidates(text):
        parsed = _load_json_candidate(candidate)
        if parsed:
            return parsed
    return {}


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(text))
    candidates.extend(_balanced_json_objects(text))
    candidates.append(text.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        deduped.append(candidate)
        seen.add(candidate)
    return deduped


def _balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return list(reversed(objects))


def _load_json_candidate(candidate: str) -> dict[str, Any]:
    for loader in (_strict_json_loads, _repair_json_loads):
        value = loader(candidate)
        if not isinstance(value, dict):
            continue
        try:
            return DiagnosisOutput.model_validate(value).model_dump()
        except ValidationError:
            continue
    return {}


def _strict_json_loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _repair_json_loads(candidate: str) -> Any:
    try:
        from json_repair import loads as repair_loads
    except Exception:
        return None
    try:
        return repair_loads(candidate)
    except Exception:
        return None


def _build_diagnosis_result(
    agent_name: str,
    vendor: str,
    model: str,
    structured: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    token_counts: dict[str, int] | None = None,
) -> DiagnosisResult:
    """Build a successful ``DiagnosisResult`` from parsed structured output."""
    return build_result(
        agent_name,
        structured,
        metadata={
            "provider": vendor,
            "model": model,
            "runtime": "deepagents+mcp",
            "tool_calls": tool_calls,
            **dict(token_counts or {}),
        },
    )


def _error_result(
    agent_name: str,
    vendor: str,
    model: str,
    exc: BaseException,
    tool_calls: list[dict[str, Any]] | None = None,
    token_counts: dict[str, int] | None = None,
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
