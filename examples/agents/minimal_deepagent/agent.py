"""Multi-vendor DeepAgent example for SDK users.

This file contains the ``MinimalDeepAgent`` class — the public agent interface
that users pass into ``bench.sessions.run_*``.

Vendor-specific LLM configuration lives in the ``providers/`` sub-package:

- ``providers/kimi.py``      — Kimi K2.6
- ``providers/minimax.py``   — MiniMax-M2.7
- ``providers/glm.py``       — ZhipuAI GLM-5.1
- ``providers/deepseek.py``  — DeepSeek deepseek-v4-pro (thinking mode disabled)
- ``providers/openai.py``    — OpenAI-compatible endpoint (gpt-5.4)

The shared output schema (``DiagnosisOutput``) lives in ``schema.py``.
Shared helper functions live in ``providers/base.py``.

Supported vendors (via ``vendor`` parameter):
- ``kimi``      — Kimi K2.6
- ``minimax``   — MiniMax-M2.7 (default)
- ``zhipu``    — GLM-5.1 (ZhipuAI)
- ``deepseek`` — deepseek-v4-pro (DeepSeek)
- ``openai``   — gpt-5.4 via OpenAI-compatible endpoint

Dependencies (install with ``pip install deepagents langchain-openai langchain-mcp-adapters``):
- deepagents
- langchain-openai
- langchain-mcp-adapters
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, Optional

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.structured_output import ToolStrategy

from netopsbench.agents.base import DiagnosticContext
from netopsbench.sdk.agents import DiagnosisResult
from netopsbench.sdk.mcp import builtin_mcp_server_config

from .prompts import DEFAULT_SYSTEM_PROMPT, build_user_prompt
from .providers import get_provider
from .providers.results import (
    _build_diagnosis_result,
    _error_result,
    _parse_raw_result,
)
from .providers.runtime import RuntimeTraceCollector, _connect_mcp_tools
from .schema import DiagnosisOutput

_PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_MAX_TOOL_CALLS = 40


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MinimalDeepAgent:
    """Thin public agent that users pass into ``NetOpsBench(...).sessions.run_*``.

    Use ``vendor`` to switch between LLM providers::

        agent = MinimalDeepAgent(vendor="kimi")     # Kimi K2.6
        agent = MinimalDeepAgent(vendor="minimax")  # MiniMax-M2.7 (default)
        agent = MinimalDeepAgent(vendor="zhipu")    # GLM-5.1
        agent = MinimalDeepAgent(vendor="deepseek") # deepseek-v4-pro
        agent = MinimalDeepAgent(vendor="openai")   # gpt-5.4

    Explicit ``model``, ``base_url``, or ``api_key`` kwargs override the
    vendor preset.
    """

    def __init__(
        self,
        *,
        name: str = "minimal-deepagent",
        vendor: str = "minimax",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        mcp_server_config: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        provider = get_provider(vendor)  # raises ValueError for unknown vendor

        self.name = name
        self.vendor = vendor
        self._provider = provider
        self.model = model or provider.PRESET["model"]
        self.base_url = base_url or provider.PRESET["base_url"]
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_tool_calls = max_tool_calls
        self.system_prompt = system_prompt
        self.api_key = api_key or os.environ.get(provider.PRESET["api_key_env"], "")
        self.mcp_server_config = mcp_server_config

    async def diagnose(self, context: DiagnosticContext) -> DiagnosisResult:
        if not self.api_key:
            raise ValueError(
                f"{self._provider.PRESET['api_key_env']} is required. "
                "Pass api_key explicitly or set it in the environment."
            )

        repo_root = Path(__file__).resolve().parents[3]
        worker_env = context.metadata.get("worker_env") if context.metadata else None
        server_config = self.mcp_server_config or builtin_mcp_server_config(workspace=repo_root, env=worker_env)
        exit_stack = AsyncExitStack()
        trace = RuntimeTraceCollector()

        try:
            await exit_stack.__aenter__()
            mcp_tools = await _connect_mcp_tools(exit_stack, server_config)

            llm = self._provider.build_llm(
                model=self.model,
                api_key=self.api_key,
                base_url=self.base_url,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout_seconds=self.timeout_seconds,
            )
            skills_root = _PACKAGE_ROOT / "skills"

            agent = create_deep_agent(
                model=llm,
                tools=mcp_tools,
                system_prompt=self.system_prompt,
                response_format=ToolStrategy(schema=DiagnosisOutput),
                skills=["/skills/"] if skills_root.is_dir() else [],
                backend=FilesystemBackend(root_dir=_PACKAGE_ROOT, virtual_mode=True),
            )

            # recursion_limit: each LLM call + tool execution = 2 graph steps,
            # so limit = 2 * max_tool_calls + 1 (for the final structured output call)
            recursion_limit = 2 * self.max_tool_calls + 1

            raw = await agent.ainvoke(
                {"messages": [{"role": "user", "content": build_user_prompt(context)}]},
                config={"recursion_limit": recursion_limit, "callbacks": [trace]},
            )
            structured, tool_calls, token_counts = _parse_raw_result(raw)
            if trace.tool_calls:
                tool_calls = trace.tool_calls
            if trace.token_counts.get("llm_call_count", 0):
                token_counts = trace.token_counts
            if not structured:
                raise ValueError("structured_response missing from runtime result")

            return _build_diagnosis_result(
                self.name,
                self.vendor,
                self.model,
                structured,
                tool_calls,
                token_counts=token_counts,
            )
        except Exception as exc:
            return _error_result(
                self.name,
                self.vendor,
                self.model,
                exc,
                tool_calls=trace.tool_calls,
                token_counts=trace.token_counts,
            )
        finally:
            await exit_stack.aclose()

    async def aclose(self) -> None:
        return None

    def get_capabilities(self):
        return ["deepagent_reasoning", "mcp_tool_diagnosis", "sdk_public_agent"]
