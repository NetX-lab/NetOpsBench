"""Provider registry for minimal_deepagent.

Each sub-module exposes two items:
- ``PRESET`` (dict) — default model, base_url, and api_key_env for the vendor.
- ``build_llm(model, api_key, base_url, temperature, max_tokens, timeout_seconds)``
  — returns a configured ``ChatOpenAI`` instance ready to use with deepagents.

Usage::

    from examples.agents.minimal_deepagent.providers import get_provider

    provider = get_provider("deepseek")
    llm = provider.build_llm(model=provider.PRESET["model"], ...)
"""

from __future__ import annotations

from types import ModuleType
from typing import Dict

from . import deepseek, glm, kimi, minimax, openai as openai_provider

_REGISTRY: Dict[str, ModuleType] = {
    "kimi": kimi,
    "minimax": minimax,
    "zhipu": glm,
    "deepseek": deepseek,
    "openai": openai_provider,
}


def get_provider(vendor: str) -> ModuleType:
    """Return the provider module for *vendor*.

    Raises ``ValueError`` for unrecognised vendor names.
    """
    provider = _REGISTRY.get(vendor)
    if provider is None:
        raise ValueError(
            f"Unknown vendor {vendor!r}. Choose from: {sorted(_REGISTRY)}"
        )
    return provider
