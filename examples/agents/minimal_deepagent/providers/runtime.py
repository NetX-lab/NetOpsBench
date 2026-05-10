"""Runtime helpers for the minimal_deepagent example."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Dict, List

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_mcp_adapters.sessions import create_session
from langchain_mcp_adapters.tools import load_mcp_tools

from .results import _message_attr, _token_usage_from_message


class RuntimeTraceCollector(BaseCallbackHandler):
    """Accumulate token and tool usage during agent execution."""

    def __init__(self) -> None:
        self._token_counts = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "llm_call_count": 0,
        }
        self._tool_calls: List[Dict[str, Any]] = []

    def on_llm_end(self, response: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any) -> None:
        del run_id, parent_run_id, kwargs
        generations = getattr(response, "generations", None) or []
        for group in generations:
            for generation in group or []:
                message = _message_attr(generation, "message")
                if message is None:
                    continue
                usage = _token_usage_from_message(message)
                self._token_counts["input_tokens"] += usage["input_tokens"]
                self._token_counts["output_tokens"] += usage["output_tokens"]
                self._token_counts["total_tokens"] += usage["total_tokens"]
                self._token_counts["llm_call_count"] += usage["has_usage"]

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del input_str, run_id, parent_run_id, tags, metadata, inputs
        tool_name = kwargs.get("name")
        if tool_name is None and isinstance(serialized, dict):
            tool_name = serialized.get("name")
        self._tool_calls.append({"tool": tool_name or "mcp_tool", "args": {}})

    @property
    def token_counts(self) -> Dict[str, int]:
        return dict(self._token_counts)

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        return [dict(tool_call) for tool_call in self._tool_calls]


async def _connect_mcp_tools(
    exit_stack: AsyncExitStack,
    server_config: Dict[str, Dict[str, Any]],
) -> list:
    """Open MCP sessions and return the loaded tool list."""
    tools: list = []
    prefix = len(server_config) > 1
    for name, connection in server_config.items():
        session = await exit_stack.enter_async_context(create_session(connection))
        await session.initialize()
        tools.extend(await load_mcp_tools(session, server_name=name, tool_name_prefix=prefix))
    return tools