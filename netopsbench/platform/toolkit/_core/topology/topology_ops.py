"""Topology-specific AgentToolkit methods."""

from __future__ import annotations

import json

from ..common import ToolResult


class TopologyOpsMixin:
    def reload_topology(self, topology_metadata: dict = None, metadata_file: str = None) -> ToolResult:
        """
        Reload topology configuration at runtime.

        Args:
            topology_metadata: Direct metadata dictionary
            metadata_file: Path to metadata JSON file

        Returns:
            ToolResult indicating success or failure
        """
        try:
            if topology_metadata:
                self._load_topology_metadata(topology_metadata)
            elif metadata_file:
                with open(metadata_file) as f:
                    self._load_topology_metadata(json.load(f))
            else:
                return ToolResult(
                    success=False, data=None, error="Either topology_metadata or metadata_file must be provided"
                )

            return ToolResult(
                success=True,
                data={
                    "topology_name": self.topology_name,
                    "devices": list(self.container_names.keys()),
                    "total_devices": len(self.container_names),
                },
            )
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_topology(self) -> ToolResult:
        """
        Get the network topology information.

        Returns topology structure including devices, links, and IP assignments.
        Requires runtime topology metadata to be loaded during toolkit initialization.
        """
        try:
            if self.topology_metadata:
                return ToolResult(success=True, data=self._enrich_topology_metadata(self.topology_metadata))
            return ToolResult(
                success=False,
                data=None,
                error="Topology metadata is not loaded; initialize toolkit with generated topology metadata.",
            )
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
