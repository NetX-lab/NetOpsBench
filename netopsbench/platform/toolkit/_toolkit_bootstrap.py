"""Bootstrap mixin for AgentToolkit runtime state."""

from __future__ import annotations

import json
import os

from netopsbench.config import config
from netopsbench.platform.topology.topology_utils import build_topology_state_from_metadata


class AgentToolkitBootstrapMixin:
    def __init__(
        self,
        grafana_url: str = "http://localhost:3000",
        grafana_user: str = "admin",
        grafana_password: str = "admin",
        influxdb_url: str = config.influxdb_url,
        influxdb_token: str = config.influxdb_token,
        influxdb_org: str = config.influxdb_org,
        influxdb_bucket: str = config.influxdb_bucket,
        topology_file: str = None,
        topology_metadata: dict = None,
    ):
        self.grafana_url = config.grafana_url or grafana_url
        self.grafana_auth = (grafana_user, grafana_password)
        self.influxdb_url = config.influxdb_url or influxdb_url
        self.influxdb_token = config.influxdb_token or influxdb_token
        self.influxdb_org = config.influxdb_org or influxdb_org
        self.influxdb_bucket = config.influxdb_bucket or influxdb_bucket

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        auto_topology_dir = self._discover_topology_dir(base_dir)
        self.topology_file = topology_file or os.path.join(auto_topology_dir, "dcn.clab.yaml")

        self.device_mgmt_ips = {}
        self.container_names = {}
        self.topology_metadata = None
        self.topology_name = "dcn"
        self.topology_id = config.topology_id

        if topology_metadata:
            build_topology_state_from_metadata(topology_metadata).apply_to(self)
        else:
            metadata_file = os.path.join(auto_topology_dir, "topology.json")
            if os.path.exists(metadata_file):
                with open(metadata_file, encoding="utf-8") as handle:
                    build_topology_state_from_metadata(json.load(handle)).apply_to(self)
            else:
                raise FileNotFoundError(
                    f"Topology metadata not found: {metadata_file}. "
                    "Set NETOPSBENCH_TOPOLOGY_DIR to a generated topology directory."
                )
        if not self.topology_id and self.topology_file:
            self.topology_id = os.path.basename(os.path.dirname(self.topology_file))

        self.screenshot_dir = os.path.join(base_dir, "screenshots")
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self.panel_mapping = {
            "bgp_established": 20,
            "bgp_not_established": 21,
            "syslog_warnings": 22,
            "syslog_total": 23,
            "bgp_timeline": 24,
            "interface_in_throughput": 1,
            "interface_out_throughput": 3,
            "syslog_events": 2,
            "syslog_by_severity": 25,
            "bgp_neighbors_table": 5,
            "interface_packets": 4,
            "queue_drops": 9,
            "physical_errors": 10,
            "logical_discards": 26,
            "pingmesh_heatmap": 100,
            "pingmesh_drops": 101,
            "rack_latency": 102,
            "path_comparison": 103,
        }
