"""Unified result type for platform operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OperationResult:
    """Standard result for platform operations that may succeed or fail.

    Use for orchestration-level operations (topology reload, deployment, etc.).
    Fault handlers return richer domain-specific dicts and are not required
    to use this type.

    Supports dict-style access (``result["success"]``) for backward
    compatibility with call sites that previously consumed raw dicts.
    """

    success: bool
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "success":
            return self.success
        if key == "error":
            return self.error
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key == "success":
            return self.success
        if key == "error":
            return self.error
        if key in self.data:
            return self.data[key]
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        payload = {"success": self.success, "error": self.error}
        payload.update(dict(self.data or {}))
        return payload
