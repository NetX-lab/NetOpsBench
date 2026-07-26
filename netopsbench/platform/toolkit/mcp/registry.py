from __future__ import annotations

import inspect
from collections import OrderedDict
from typing import Any, get_type_hints

from pydantic import TypeAdapter, ValidationError

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


def tool_schemas() -> list[dict[str, Any]]:
    """Return the model-facing JSON schema for the canonical toolkit registry."""
    schemas: list[dict[str, Any]] = []
    for spec in load_tool_specs():
        signature = inspect.signature(spec.handler)
        hints = get_type_hints(spec.handler)
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for name, parameter in signature.parameters.items():
            if name == "self":
                continue
            properties[name] = TypeAdapter(hints.get(name, Any)).json_schema()
            if parameter.default is inspect.Parameter.empty:
                required.append(name)
            else:
                properties[name]["default"] = parameter.default
        description = (inspect.getdoc(spec.handler) or "").splitlines()[0]
        schemas.append(
            {
                "name": spec.name,
                "group": spec.group,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return schemas


def validate_tool_call(name: str, arguments: dict[str, Any]) -> bool:
    """Validate a tool call against the same typed registry used by FastMCP."""
    spec = next((candidate for candidate in load_tool_specs() if candidate.name == name), None)
    if spec is None:
        return False
    signature = inspect.signature(spec.handler)
    hints = get_type_hints(spec.handler)
    parameters = {key: value for key, value in signature.parameters.items() if key != "self"}
    if set(arguments) - set(parameters):
        return False
    for key, parameter in parameters.items():
        if parameter.default is inspect.Parameter.empty and key not in arguments:
            return False
        if key in arguments:
            try:
                TypeAdapter(hints.get(key, Any)).validate_python(arguments[key], strict=True)
            except ValidationError:
                return False
    return True


__all__ = ["group_tool_names", "load_tool_specs", "tool_schemas", "validate_tool_call"]
