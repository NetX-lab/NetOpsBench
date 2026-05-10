"""DeepSeek provider configuration and LLM factory.

Key difference from other providers: thinking mode is explicitly disabled via
``extra_body={"thinking": {"type": "disabled"}}``.

deepseek-v4-pro defaults to thinking (reasoning) mode.  In thinking mode, every
assistant message carries a ``reasoning_content`` field that *must* be echoed back
in the next API call, or the API returns HTTP 400.  The deepagents framework does
not preserve this field when building the next-turn message list, which caused the
400 errors in earlier runs (run-0008, run-0009).

Disabling thinking mode makes deepseek-v4-pro behave identically to a plain
OpenAI-compatible chat model: standard tool_calls / ToolStrategy work without any
special handling.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

PRESET = {
    "model": "deepseek-v4-pro",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
}


def build_llm(
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
) -> ChatOpenAI:
    """Return a ChatOpenAI instance with DeepSeek thinking mode disabled.

    ``model_kwargs`` is forwarded verbatim to every API request, so
    ``extra_body`` ensures ``thinking.type=disabled`` on all calls.
    """
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        max_retries=4,
        extra_body={"thinking": {"type": "disabled"}},
    )
