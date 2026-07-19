"""Deterministic size bounds for simulator observations."""

from __future__ import annotations

import json
from typing import Any


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def compact_json(value: Any, max_bytes: int) -> Any:
    """Keep complete JSON values while bounding an agent-facing payload."""
    original_bytes = json_size(value)
    if original_bytes <= max_bytes:
        return value
    marker = {"truncated": True, "original_bytes": original_bytes}
    compacted = _compact(value, max_bytes, marker)
    if json_size(compacted) <= max_bytes:
        return compacted
    return {"_truncation": marker}


def _compact(value: Any, budget: int, marker: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        dict_output: dict[str, Any] = {}
        items = sorted(value.items(), key=lambda item: str(item[0]))
        for index, (key, child) in enumerate(items):
            remaining = max(128, budget - json_size(dict_output) - 256)
            dict_output[str(key)] = _compact(child, remaining, marker)
            candidate = {**dict_output, "_truncation": {**marker, "omitted_items": len(items) - index - 1}}
            if json_size(candidate) > budget:
                dict_output.pop(str(key), None)
                break
        dict_output["_truncation"] = {**marker, "omitted_items": len(items) - len(dict_output)}
        return dict_output
    if isinstance(value, list):
        list_output: list[Any] = []
        for index, child in enumerate(value):
            remaining = max(128, budget - json_size(list_output) - 256)
            list_output.append(_compact(child, remaining, marker))
            list_candidate = [*list_output, {"_truncation": {**marker, "omitted_items": len(value) - index - 1}}]
            if json_size(list_candidate) > budget:
                list_output.pop()
                break
        list_output.append({"_truncation": {**marker, "omitted_items": len(value) - len(list_output)}})
        return list_output
    if isinstance(value, str):
        prefix_bytes = max(0, budget - 160)
        prefix = value.encode("utf-8")[:prefix_bytes].decode("utf-8", errors="ignore")
        return {"_truncated_text": prefix, "original_chars": len(value)}
    return value


__all__ = ["compact_json", "json_size"]
