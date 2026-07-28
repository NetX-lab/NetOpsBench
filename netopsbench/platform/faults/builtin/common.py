"""Shared parameter lookup for builtin fault declarations."""

from __future__ import annotations

from typing import Any


def episode_param(episode: Any, key: str, default=None):
    parameters = getattr(episode, "parameters", None) or {}
    if isinstance(parameters, dict) and key in parameters:
        return parameters.get(key, default)
    metadata = getattr(episode, "metadata", None) or {}
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return default


__all__ = ["episode_param"]
