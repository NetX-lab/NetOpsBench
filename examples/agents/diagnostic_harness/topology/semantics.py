"""Topology-family-neutral names used inside the diagnostic harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

AttachmentSide = Literal["source", "destination"]

_ATTACHMENT_KEYS: dict[AttachmentSide, tuple[str, ...]] = {
    "source": ("src_attachment", "source_attachment", "src_leaf", "source_leaf"),
    "destination": ("dst_attachment", "destination_attachment", "dst_leaf", "destination_leaf"),
}


def attachment_from_metadata(metadata: Mapping[str, Any], side: AttachmentSide) -> str | None:
    """Read the neutral attachment field, accepting legacy Pingmesh names."""
    for key in _ATTACHMENT_KEYS[side]:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return None


def with_attachment_aliases(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Add neutral aliases without changing the public Pingmesh contract."""
    result = dict(metadata)
    source = attachment_from_metadata(result, "source")
    destination = attachment_from_metadata(result, "destination")
    if source:
        if not result.get("src_attachment"):
            result["src_attachment"] = source
        if not result.get("source_attachment"):
            result["source_attachment"] = source
    if destination:
        if not result.get("dst_attachment"):
            result["dst_attachment"] = destination
        if not result.get("destination_attachment"):
            result["destination_attachment"] = destination
    return result


__all__ = ["attachment_from_metadata", "with_attachment_aliases"]
