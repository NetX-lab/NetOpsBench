"""Agent toolkit public entrypoint."""

from __future__ import annotations

from ._core.common import ToolResult
from ._core.device.device_ops import DeviceOpsMixin
from ._core.observability.observability_facade import ObservabilityFacadeMixin
from ._core.topology.topology_ops import TopologyOpsMixin
from ._toolkit_bootstrap import AgentToolkitBootstrapMixin
from ._toolkit_helpers import AgentToolkitHelperMixin


class AgentToolkit(
    AgentToolkitBootstrapMixin, AgentToolkitHelperMixin, TopologyOpsMixin, DeviceOpsMixin, ObservabilityFacadeMixin
):
    """Toolkit providing network troubleshooting capabilities to the AI Agent."""


__all__ = ["AgentToolkit", "ToolResult"]
