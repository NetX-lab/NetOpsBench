"""Shared parameter lookup for builtin fault declarations."""

from __future__ import annotations

from typing import Any


def episode_param(episode: Any, key: str, default=None):
    return episode.parameters.get(key, default)


__all__ = ["episode_param"]
