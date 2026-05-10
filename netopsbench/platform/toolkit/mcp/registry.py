from collections import OrderedDict

from .contracts import ToolSpec


def load_tool_specs() -> list[ToolSpec]:
    from .connectivity import TOOL_SPECS as connectivity_tools
    from .inventory import TOOL_SPECS as inventory_tools
    from .observability import TOOL_SPECS as observability_tools

    merged = list(inventory_tools) + list(observability_tools) + list(connectivity_tools)

    names = [spec.name for spec in merged]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate MCP tool names detected in toolkit registry")
    return merged


def group_tool_names(tool_specs: list[ToolSpec]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = OrderedDict()
    for spec in tool_specs:
        grouped.setdefault(spec.group, []).append(spec.name)
    return grouped
