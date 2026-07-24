"""Small crash-safe filesystem helpers for platform-owned JSON artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` atomically with fully flushed text."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: str | Path, payload: Any, *, default: Any = None) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, default=default, ensure_ascii=False),
    )


__all__ = ["atomic_write_json", "atomic_write_text"]
