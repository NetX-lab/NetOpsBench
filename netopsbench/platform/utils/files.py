"""Small crash-safe filesystem helpers for platform-owned JSON artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` atomically with fully flushed text."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Replace an owned artifact only after its complete contents are flushed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
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


__all__ = ["atomic_write_bytes", "atomic_write_json", "atomic_write_text"]
