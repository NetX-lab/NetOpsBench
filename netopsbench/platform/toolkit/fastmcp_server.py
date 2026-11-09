"""FastMCP server exposing the single canonical toolkit registry."""

import asyncio
import inspect
from functools import wraps
from typing import get_type_hints

from .mcp.registry import group_tool_names, load_tool_specs

_TOOL_SPECS = load_tool_specs()
for _spec in _TOOL_SPECS:
    globals()[_spec.name] = _spec.handler

EXPOSED_TOOLS = [_spec.name for _spec in _TOOL_SPECS]
EXPOSED_TOOLS_BY_GROUP = group_tool_names(_TOOL_SPECS)


def create_server(*, gateway=None, auth=None, trace=None, max_tool_calls=None):
    """Create the optional FastMCP server only when it is actually used."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "fastmcp is not installed. Install with: pip install -e '.[agent]' or pip install -e '.[dev,agent]'"
        ) from exc
    server = FastMCP("netopsbench", auth=auth)
    budget = {"calls": 0}
    lock = asyncio.Lock()

    def bind(spec):
        @wraps(spec.handler)
        async def invoke(**kwargs):
            async with lock:
                if max_tool_calls is not None and budget["calls"] >= max_tool_calls:
                    raise RuntimeError("MCP tool-call budget exhausted")
                budget["calls"] += 1
                call_id = trace.record_tool_start(name=spec.name, args=kwargs) if trace else None
                try:
                    result = await asyncio.to_thread(getattr(gateway, spec.name), **kwargs)
                    if trace:
                        trace.record_tool_end(output=result.to_dict(), run_id=call_id)
                    return result.to_dict()
                except Exception as exc:
                    if trace:
                        trace.record_tool_error(error=exc, run_id=call_id)
                    raise

        hints = get_type_hints(spec.handler)
        invoke.__signature__ = inspect.signature(spec.handler).replace(
            parameters=[
                parameter.replace(annotation=hints.get(name, parameter.annotation))
                for name, parameter in inspect.signature(spec.handler).parameters.items()
            ],
            return_annotation=dict,
        )
        invoke.__annotations__ = {**hints, "return": dict}
        return invoke

    for spec in _TOOL_SPECS:
        server.tool(name=spec.name)(bind(spec) if gateway is not None else spec.handler)
    return server


def run_server():
    """Entry point used by CLI command."""
    create_server().run(transport="stdio")


def main() -> int:
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
