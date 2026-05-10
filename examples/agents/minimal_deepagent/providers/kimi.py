"""Kimi provider configuration and LLM factory.

Kimi K2.6 constraints (live chat API):
- thinking=enabled requires ``temperature=1`` but breaks ``tool_choice='required'``.
- thinking=disabled requires ``temperature=0.6`` and allows tool_choice.
- We always disable thinking so MCP tool-calling works correctly.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

PRESET = {
    "model": "kimi-k2.6",
    "base_url": "https://api.moonshot.cn/v1",
    "api_key_env": "KIMI_API_KEY",
}


def build_llm(
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
) -> ChatOpenAI:
    # kimi-k2.6: thinking=enabled requires temperature=1;
    #            thinking=disabled requires temperature=0.6.
    # We disable thinking so tool_choice='required' works, and use 0.6.
    del temperature
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.6,
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        max_retries=4,
        extra_body={"thinking": {"type": "disabled"}},
    )