"""ZhipuAI GLM provider configuration and LLM factory."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

PRESET = {
    "model": "glm-5.1",
    "base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key_env": "ZHIPU_API_KEY",
}


def build_llm(
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout_seconds,
        max_retries=4,
    )
