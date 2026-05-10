"""Structured models for fault runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveFault:
    """Normalized representation of an active injected fault."""

    type: str
    device: str | None = None
    interface: str | None = None
    success: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "success": self.success,
            "error": self.error,
        }
        if self.device is not None:
            payload["device"] = self.device
        if self.interface is not None:
            payload["interface"] = self.interface
        payload.update(dict(self.metadata or {}))
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActiveFault:
        payload = dict(payload or {})
        metadata = dict(payload)
        fault_type = metadata.pop("type")
        device = metadata.pop("device", None)
        interface = metadata.pop("interface", None)
        success = metadata.pop("success", True)
        error = metadata.pop("error", None)
        return cls(
            type=fault_type,
            device=device,
            interface=interface,
            success=success,
            error=error,
            metadata=metadata,
        )
