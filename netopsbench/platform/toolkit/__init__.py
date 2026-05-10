"""Toolkit interfaces for NetOpsBench platform internals.

External integrations should use SDK MCP helpers from ``netopsbench.sdk.mcp``.
This module remains importable for internal platform code and tests.
"""

from ._core.common import ToolResult
from .toolkit import AgentToolkit

__all__ = ["AgentToolkit", "ToolResult"]
