#!/usr/bin/env python3
"""FastMCP server exposing NetOpsBench toolkit tools."""

import importlib

try:
    FastMCP = importlib.import_module("fastmcp").FastMCP
except Exception:

    class FastMCP:  # type: ignore[override]
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def run(self, *args, **kwargs):
            raise RuntimeError(
                "fastmcp is not installed. Install with: pip install -e '.[agent]' or pip install -e '.[dev,agent]'"
            )


from .mcp.registry import group_tool_names, load_tool_specs

mcp = FastMCP("netopsbench")

_TOOL_SPECS = load_tool_specs()
for _spec in _TOOL_SPECS:
    globals()[_spec.name] = _spec.handler
    mcp.tool(name=_spec.name)(_spec.handler)

EXPOSED_TOOLS = [_spec.name for _spec in _TOOL_SPECS]
EXPOSED_TOOLS_BY_GROUP = group_tool_names(_TOOL_SPECS)


def run_server():
    """Entry point used by CLI command."""
    mcp.run(transport="stdio")
