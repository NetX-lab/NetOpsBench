"""TTL cache with stable, parameter-normalized tool keys."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def _normalized_parameters(parameters: dict[str, Any] | None) -> str:
    return json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class ToolCacheKey:
    tool_name: str
    device: str | None
    interface: str | None
    normalized_parameters: str


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLToolCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: dict[ToolCacheKey, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(
        tool_name: str,
        *,
        device: str | None = None,
        interface: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> ToolCacheKey:
        return ToolCacheKey(
            tool_name=str(tool_name),
            device=str(device) if device is not None else None,
            interface=str(interface) if interface is not None else None,
            normalized_parameters=_normalized_parameters(parameters),
        )

    def set(self, key: ToolCacheKey, value: Any, *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            self._items.pop(key, None)
            return
        self._items[key] = _CacheEntry(value=value, expires_at=self._clock() + ttl_seconds)

    def get(self, key: ToolCacheKey) -> Any | None:
        item = self._items.get(key)
        if item is None:
            self.misses += 1
            return None
        if item.expires_at <= self._clock():
            self._items.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return item.value

    def has(self, key: ToolCacheKey) -> bool:
        item = self._items.get(key)
        return item is not None and item.expires_at > self._clock()

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._items)}


__all__ = ["TTLToolCache", "ToolCacheKey"]
