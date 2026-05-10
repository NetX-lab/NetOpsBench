"""Shared agent toolkit types."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ToolResult:
    """Result from a tool invocation."""

    success: bool
    data: Any
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def truncate_text_lines(text: str, max_lines: int | None) -> tuple[str, dict]:
    """Return text capped to max_lines plus truncation metadata."""
    lines = (text or "").splitlines()
    total_lines = len(lines)
    if max_lines is None:
        return text or "", {"truncated": False, "returned_lines": total_lines, "total_lines": total_lines}
    safe_max_lines = max(1, int(max_lines))
    if total_lines <= safe_max_lines:
        return text or "", {"truncated": False, "returned_lines": total_lines, "total_lines": total_lines}
    return "\n".join(lines[:safe_max_lines]), {
        "truncated": True,
        "returned_lines": safe_max_lines,
        "total_lines": total_lines,
    }
