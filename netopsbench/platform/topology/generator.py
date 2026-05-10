"""
Topology Generator - Generate DCN network topologies for Containerlab.

Supports generating 2-tier CLOS topologies with configurable scale.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass

import yaml

DEFAULT_SONIC_VS_IMAGE = "yyyyyt123/netopsbench-sonic-vs-202505-telemetry:202505-telemetry"


@dataclass
class TopologyConfig:
    """Configuration for topology generation."""

    name: str = "dcn"
    num_spines: int = 2
    num_leafs: int = 2
    clients_per_leaf: int = 1
    nos_kind: str = "sonic-vs"
    nos_image: str = DEFAULT_SONIC_VS_IMAGE
    client_image: str = "yyyyyt123/netopsbench-client:python3"
    mgmt_ipv4_subnet: str = "172.20.20.0/24"
    mgmt_network_name: str | None = None
    collector_ip: str | None = None
    spine_asn: int = 65001
    leaf_asn_start: int = 65011


# Predefined topology scales
TOPOLOGY_SCALES = {
    "xs": TopologyConfig(num_spines=2, num_leafs=2, clients_per_leaf=1),
    "small": TopologyConfig(num_spines=2, num_leafs=4, clients_per_leaf=2),
    "medium": TopologyConfig(num_spines=4, num_leafs=8, clients_per_leaf=2),
    "large": TopologyConfig(num_spines=4, num_leafs=16, clients_per_leaf=4),
}


class TopologyGenerator:
    """
    Generates DCN network topologies for Containerlab.

    Creates CLOS topology YAML files and device configurations.
    """

    def __init__(self, config: TopologyConfig | None = None, output_dir: str | None = None):
        self.config = config or TopologyConfig()
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "generated_topology",
        )
        self.mgmt_network = ipaddress.ip_network(self.config.mgmt_ipv4_subnet, strict=False)
        if self.mgmt_network.version != 4:
            raise ValueError(f"Only IPv4 management subnets are supported: {self.config.mgmt_ipv4_subnet}")
        self.mgmt_network_name = self.config.mgmt_network_name or f"clab-mgmt-{self.config.name}"

        # Calculate IP offsets to avoid collisions
        self.spine_mgmt_offset = 10
        self.leaf_mgmt_offset = self.spine_mgmt_offset + self.config.num_spines
        self.client_mgmt_offset = self.leaf_mgmt_offset + self.config.num_leafs
        self.collector_ip = self.config.collector_ip or self._default_collector_ip()

        self.syslog_collector = os.getenv("NETOPSBENCH_SYSLOG_COLLECTOR", self.collector_ip)
        self.sflow_collector = os.getenv("NETOPSBENCH_SFLOW_COLLECTOR", self.syslog_collector)
        self.sflow_port = int(os.getenv("NETOPSBENCH_SFLOW_PORT", "6343"))
        self.sflow_polling_interval = int(os.getenv("NETOPSBENCH_SFLOW_POLLING_INTERVAL", "20"))
        self.sflow_sample_rate = int(os.getenv("NETOPSBENCH_SFLOW_SAMPLE_RATE", "1000"))
        self.sflow_sample_direction = os.getenv("NETOPSBENCH_SFLOW_SAMPLE_DIRECTION", "ingress")
        self.snmp_community = os.getenv("NETOPSBENCH_SNMP_COMMUNITY", "public")

    def _mgmt_host_ip(self, host_offset: int) -> str:
        """Return a deterministic management IPv4 address from the configured subnet."""
        if host_offset <= 0:
            raise ValueError(f"Management host offset must be positive, got {host_offset}")

        candidate = ipaddress.ip_address(int(self.mgmt_network.network_address) + host_offset)
        if candidate not in self.mgmt_network:
            raise ValueError(f"Management host offset {host_offset} falls outside subnet {self.mgmt_network}")
        if candidate in {self.mgmt_network.network_address, self.mgmt_network.broadcast_address}:
            raise ValueError(f"Management host offset {host_offset} resolves to reserved address {candidate}")
        return str(candidate)

    def _default_collector_ip(self) -> str:
        """Pick a collector IP inside the management subnet, preferring .200 when possible."""
        preferred = int(self.mgmt_network.network_address) + 200
        preferred_ip = ipaddress.ip_address(preferred)
        if preferred_ip in self.mgmt_network and preferred_ip not in {
            self.mgmt_network.network_address,
            self.mgmt_network.broadcast_address,
        }:
            return str(preferred_ip)

        fallback = ipaddress.ip_address(int(self.mgmt_network.broadcast_address) - 1)
        if fallback in {self.mgmt_network.network_address, self.mgmt_network.broadcast_address}:
            raise ValueError(f"Management subnet too small for collector IP: {self.mgmt_network}")
        return str(fallback)

    def generate(self) -> dict:
        """
        Generate complete topology including YAML and metadata.

        Returns:
            Dictionary containing topology metadata and file paths
        """
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "configs"), exist_ok=True)

        # Generate topology structure
        topology = self._generate_topology_structure()

        # Generate YAML file
        yaml_path = self._write_yaml(topology)

        # Generate device configs
        config_paths = self._generate_device_configs()

        # Generate metadata
        metadata = self._generate_metadata()
        metadata_path = os.path.join(self.output_dir, "topology.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        return {
            "yaml_file": yaml_path,
            "metadata_file": metadata_path,
            "config_files": config_paths,
            "metadata": metadata,
        }

    def _generate_topology_structure(self) -> dict:
        """Generate Containerlab topology YAML structure."""
        topology = {
            "name": self.config.name,
            "mgmt": {
                "network": self.mgmt_network_name,
                "ipv4-subnet": str(self.mgmt_network),
            },
            "topology": {
                "kinds": {
                    self.config.nos_kind: {"image": self.config.nos_image},
                    "linux": {"image": self.config.client_image},
                },
                "nodes": {},
                "links": [],
            },
        }

        nodes = topology["topology"]["nodes"]
        links = topology["topology"]["links"]

        # Generate spine nodes
        for i in range(1, self.config.num_spines + 1):
            spine_name = f"spine{i}"
            nodes[spine_name] = {
                "kind": self.config.nos_kind,
                "group": "spine",
                "mgmt-ipv4": self._mgmt_host_ip(self.spine_mgmt_offset + i),
            }

        # Generate leaf nodes and clients
        for i in range(1, self.config.num_leafs + 1):
            leaf_name = f"leaf{i}"
            nodes[leaf_name] = {
                "kind": self.config.nos_kind,
                "group": "leaf",
                "mgmt-ipv4": self._mgmt_host_ip(self.leaf_mgmt_offset + i),
            }

            # Generate clients for this leaf
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                client_name = f"client{client_idx}"
                # Point-to-point /30 subnet addressing
                # Each client gets: 192.168.{octet}.{(j-1)*4 + 2}/30
                # Gateway is:       192.168.{octet}.{(j-1)*4 + 1}
                octet = self._client_subnet_octet(i)
                subnet_base = (j - 1) * 4
                client_ip = f"192.168.{octet}.{subnet_base + 2}"
                gateway = f"192.168.{octet}.{subnet_base + 1}"

                nodes[client_name] = {
                    "kind": "linux",
                    "group": "client",
                    "mgmt-ipv4": self._mgmt_host_ip(self.client_mgmt_offset + client_idx),
                    "exec": [
                        "ip link set dev eth1 mtu 9232",
                        f"ip addr add {client_ip}/30 dev eth1",
                        f"ip route add 192.168.0.0/16 via {gateway}",
                        "mkdir -p /var/log/pingmesh",
                        "iperf3 -s -D",
                        "ethtool -K eth1 rx off tx off tso off gso off gro off sg off tx-udp-segmentation off",
                    ],
                }

        # Generate spine-leaf links
        interface_counter = {}
        for spine_idx in range(1, self.config.num_spines + 1):
            spine_name = f"spine{spine_idx}"
            for leaf_idx in range(1, self.config.num_leafs + 1):
                leaf_name = f"leaf{leaf_idx}"

                # Get next available interface for each device
                spine_if = interface_counter.get(spine_name, 0) + 1
                leaf_if = interface_counter.get(leaf_name, 0) + 1
                interface_counter[spine_name] = spine_if
                interface_counter[leaf_name] = leaf_if

                links.append({"endpoints": [f"{spine_name}:eth{spine_if}", f"{leaf_name}:eth{leaf_if}"], "mtu": 9232})

        # Generate leaf-client links
        for i in range(1, self.config.num_leafs + 1):
            leaf_name = f"leaf{i}"
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                client_name = f"client{client_idx}"

                leaf_if = interface_counter.get(leaf_name, 0) + 1
                interface_counter[leaf_name] = leaf_if

                links.append({"endpoints": [f"{leaf_name}:eth{leaf_if}", f"{client_name}:eth1"], "mtu": 9232})

        return topology

    def _write_yaml(self, topology: dict) -> str:
        """Write topology YAML file."""
        yaml_path = os.path.join(self.output_dir, f"{self.config.name}.clab.yaml")

        # Add header comment
        header = f"""# {self.config.name.upper()} Topology Configuration
# Generated by NetOpsBench Topology Generator
# Scale: {self.config.num_spines} spines, {self.config.num_leafs} leafs, {self.config.clients_per_leaf} clients/leaf
#
# Features:
# - BGP with ECMP
# - Observability: Syslog, gNMI, sFlow

"""
        with open(yaml_path, "w") as f:
            f.write(header)
            yaml.dump(topology, f, default_flow_style=False, sort_keys=False)

        return yaml_path

    def _generate_device_configs(self) -> list[str]:
        """Generate device configuration files."""
        config_paths = []

        # Generate spine configs
        for i in range(1, self.config.num_spines + 1):
            config_path = self._generate_spine_config(i)
            config_paths.append(config_path)

        # Generate leaf configs
        for i in range(1, self.config.num_leafs + 1):
            config_path = self._generate_leaf_config(i)
            config_paths.append(config_path)

        return config_paths

    def _sonic_port_name(self, eth_index: int) -> str:
        """Map Linux eth index (eth1, eth2) to SONiC port name (Ethernet0, Ethernet4, ...)."""
        return f"Ethernet{(eth_index - 1) * 4}"

    def _client_subnet_octet(self, leaf_idx: int) -> int:
        """Return client subnet /24 octet for a leaf, avoiding spine-link collisions."""
        return 100 + leaf_idx

    def _telemetry_config_lines(self) -> list[str]:
        return [
            f"config syslog add {self.syslog_collector} || true",
            "sonic-db-cli CONFIG_DB hset 'FLEX_COUNTER_TABLE|PORT' FLEX_COUNTER_STATUS enable || true",
            "sonic-db-cli CONFIG_DB hset 'FLEX_COUNTER_TABLE|PORT' POLL_INTERVAL 1000 || true",
            "sonic-db-cli CONFIG_DB hmset 'GNMI|gnmi' port 50051 client_auth false log_level 2 || true",
            (
                "sonic-db-cli CONFIG_DB hmset 'GNMI|certs' "
                "ca_crt /etc/sonic/telemetry/dsmsroot.cer "
                "server_crt /etc/sonic/telemetry/streamingtelemetryserver.cer "
                "server_key /etc/sonic/telemetry/streamingtelemetryserver.key || true"
            ),
            "mkdir -p /var/run/telemetry /var/log/telemetry || true",
            (
                "pgrep -x telemetry >/dev/null 2>&1 || "
                "nohup /usr/sbin/telemetry -port 50051 -noTLS -client_auth none "
                ">/var/log/telemetry/telemetry.log 2>&1 &"
            ),
            "config sflow agent-id add mgmt0 || true",
            f"config sflow collector add telegraf {self.sflow_collector} --port {self.sflow_port} || true",
            f"config sflow polling-interval {self.sflow_polling_interval} || true",
            f"config sflow sample-direction {self.sflow_sample_direction} || true",
            "config sflow enable || true",
        ]

    def _generate_spine_config(self, spine_idx: int) -> str:
        """Generate configuration for a spine switch."""
        spine_name = f"spine{spine_idx}"
        router_id = f"10.0.0.{spine_idx}"

        script = [
            f"# Spine {spine_idx} Configuration",
            "# Generated by NetOpsBench",
            "set -e",
            "",
        ]

        vtysh_cmds = [
            "configure terminal",
            "route-map RM-ALLOW permit 10",
            "exit",
            f"router bgp {self.config.spine_asn}",
            f"bgp router-id {router_id}",
            "no bgp ebgp-requires-policy",
        ]

        # Add interfaces and BGP neighbors for each leaf
        for leaf_idx in range(1, self.config.num_leafs + 1):
            interface = self._sonic_port_name(leaf_idx)
            subnet_idx = spine_idx * 10 + leaf_idx
            spine_ip = f"192.168.{subnet_idx}.1"
            leaf_ip = f"192.168.{subnet_idx}.2"
            leaf_asn = self.config.leaf_asn_start + leaf_idx - 1

            script.append(f"config interface startup {interface}")
            script.append(f"config interface ip add {interface} {spine_ip}/30")
            script.append(f"config sflow interface enable {interface} || true")
            script.append(f"config sflow interface sample-rate {interface} {self.sflow_sample_rate} || true")
            vtysh_cmds.append(f"neighbor {leaf_ip} remote-as {leaf_asn}")

        # Telemetry services (best-effort)
        script.extend(self._telemetry_config_lines())

        vtysh_cmds.extend(
            [
                "address-family ipv4 unicast",
            ]
        )
        for leaf_idx in range(1, self.config.num_leafs + 1):
            subnet_idx = spine_idx * 10 + leaf_idx
            leaf_ip = f"192.168.{subnet_idx}.2"
            vtysh_cmds.append(f"neighbor {leaf_ip} activate")
            vtysh_cmds.append(f"neighbor {leaf_ip} route-map RM-ALLOW out")
        vtysh_cmds.extend(
            [
                "exit-address-family",
                "end",
                "write memory",
            ]
        )

        script.append("supervisorctl start bgpd >/dev/null 2>&1 || true")
        script.append("")
        script.append("vtysh <<'VTY'")
        script.extend(vtysh_cmds)
        script.append("VTY")

        # Write config
        config_path = os.path.join(self.output_dir, "configs", f"{spine_name}.sh")
        with open(config_path, "w") as f:
            f.write("\n".join(script) + "\n")

        return config_path

    def _generate_leaf_config(self, leaf_idx: int) -> str:
        """Generate configuration for a leaf switch."""
        leaf_name = f"leaf{leaf_idx}"
        router_id = f"10.0.0.{10 + leaf_idx}"
        leaf_asn = self.config.leaf_asn_start + leaf_idx - 1

        script = [
            f"# Leaf {leaf_idx} Configuration",
            "# Generated by NetOpsBench",
            "set -e",
            "",
        ]

        vtysh_cmds = [
            "configure terminal",
            "route-map RM-ALLOW permit 10",
            "exit",
            f"router bgp {leaf_asn}",
            f"bgp router-id {router_id}",
            "no bgp ebgp-requires-policy",
        ]

        # Add interfaces to spines
        for spine_idx in range(1, self.config.num_spines + 1):
            interface = self._sonic_port_name(spine_idx)
            subnet_idx = spine_idx * 10 + leaf_idx
            spine_ip = f"192.168.{subnet_idx}.1"
            leaf_ip = f"192.168.{subnet_idx}.2"

            script.append(f"config interface startup {interface}")
            script.append(f"config interface ip add {interface} {leaf_ip}/30")
            script.append(f"config sflow interface enable {interface} || true")
            script.append(f"config sflow interface sample-rate {interface} {self.sflow_sample_rate} || true")
            vtysh_cmds.append(f"neighbor {spine_ip} remote-as {self.config.spine_asn}")

        # Add client-facing interfaces (one per client)
        # Use point-to-point /30 subnets for each client to avoid subnet overlap
        # Each client gets a dedicated /30 subnet from the leaf's /24 address space
        for client_idx in range(1, self.config.clients_per_leaf + 1):
            client_interface_idx = self.config.num_spines + client_idx
            # Calculate /30 subnet: 192.168.{octet}.{(client_idx-1)*4}/30
            # Gateway IP is first usable IP in /30: 192.168.{octet}.{(client_idx-1)*4 + 1}
            octet = self._client_subnet_octet(leaf_idx)
            subnet_base = (client_idx - 1) * 4
            gateway_ip = subnet_base + 1
            interface = self._sonic_port_name(client_interface_idx)
            script.append(f"config interface startup {interface}")
            script.append(f"config interface ip add {interface} 192.168.{octet}.{gateway_ip}/30")
            script.append(f"config sflow interface enable {interface} || true")
            script.append(f"config sflow interface sample-rate {interface} {self.sflow_sample_rate} || true")

        # Telemetry services (best-effort)
        script.extend(self._telemetry_config_lines())

        vtysh_cmds.extend(
            [
                "address-family ipv4 unicast",
            ]
        )
        for spine_idx in range(1, self.config.num_spines + 1):
            subnet_idx = spine_idx * 10 + leaf_idx
            spine_ip = f"192.168.{subnet_idx}.1"
            vtysh_cmds.append(f"neighbor {spine_ip} activate")
            vtysh_cmds.append(f"neighbor {spine_ip} route-map RM-ALLOW out")
        octet = self._client_subnet_octet(leaf_idx)
        for client_idx in range(1, self.config.clients_per_leaf + 1):
            subnet_base = (client_idx - 1) * 4
            vtysh_cmds.append(f"network 192.168.{octet}.{subnet_base}/30 route-map RM-ALLOW")
        vtysh_cmds.extend(
            [
                "exit-address-family",
                "end",
                "write memory",
            ]
        )

        script.append("supervisorctl start bgpd >/dev/null 2>&1 || true")
        script.append("")
        script.append("vtysh <<'VTY'")
        script.extend(vtysh_cmds)
        script.append("VTY")

        # Write config
        config_path = os.path.join(self.output_dir, "configs", f"{leaf_name}.sh")
        with open(config_path, "w") as f:
            f.write("\n".join(script) + "\n")

        return config_path

    def _generate_metadata(self) -> dict:
        """Generate topology metadata JSON."""
        devices = {"spines": [], "leafs": [], "clients": []}

        # Spines
        for i in range(1, self.config.num_spines + 1):
            devices["spines"].append(
                {
                    "name": f"spine{i}",
                    "mgmt_ip": self._mgmt_host_ip(self.spine_mgmt_offset + i),
                    "router_id": f"10.0.0.{i}",
                    "asn": self.config.spine_asn,
                }
            )

        # Leafs
        for i in range(1, self.config.num_leafs + 1):
            devices["leafs"].append(
                {
                    "name": f"leaf{i}",
                    "mgmt_ip": self._mgmt_host_ip(self.leaf_mgmt_offset + i),
                    "router_id": f"10.0.0.{10 + i}",
                    "asn": self.config.leaf_asn_start + i - 1,
                    "client_subnet": f"192.168.{self._client_subnet_octet(i)}.0/24",
                }
            )

        # Clients
        for i in range(1, self.config.num_leafs + 1):
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                # Point-to-point /30 subnet addressing
                octet = self._client_subnet_octet(i)
                subnet_base = (j - 1) * 4
                client_ip = f"192.168.{octet}.{subnet_base + 2}"
                devices["clients"].append(
                    {
                        "name": f"client{client_idx}",
                        "mgmt_ip": self._mgmt_host_ip(self.client_mgmt_offset + client_idx),
                        "data_ip": client_ip,
                        "leaf": f"leaf{i}",
                        "rack": f"rack{i}",
                    }
                )

        # Links
        links = []
        for spine_idx in range(1, self.config.num_spines + 1):
            for leaf_idx in range(1, self.config.num_leafs + 1):
                links.append({"endpoints": [f"spine{spine_idx}", f"leaf{leaf_idx}"], "type": "spine-leaf"})

        for i in range(1, self.config.num_leafs + 1):
            for j in range(1, self.config.clients_per_leaf + 1):
                client_idx = (i - 1) * self.config.clients_per_leaf + j
                links.append({"endpoints": [f"leaf{i}", f"client{client_idx}"], "type": "leaf-client"})

        return {
            "name": self.config.name,
            "management": {
                "network": self.mgmt_network_name,
                "ipv4_subnet": str(self.mgmt_network),
            },
            "collector": {
                "ipv4": self.syslog_collector,
                "sflow_port": self.sflow_port,
            },
            "defaults": {
                "link_mtu": 9232,
                "sonic_port_mtu": 9100,
            },
            "mtu_semantics": {
                "link_mtu_scope": "containerlab/client link MTU",
                "sonic_port_mtu_scope": "SONiC front-panel interface MTU",
                "note": "Do not compare link_mtu 9232 directly against healthy SONiC port MTU 9100 when diagnosing faults.",
            },
            "scale": {
                "num_spines": self.config.num_spines,
                "num_leafs": self.config.num_leafs,
                "clients_per_leaf": self.config.clients_per_leaf,
                "total_clients": self.config.num_leafs * self.config.clients_per_leaf,
                "total_devices": self.config.num_spines + self.config.num_leafs,
            },
            "devices": devices,
            "links": links,
            "routing": {
                "protocol": "BGP",
                "spine_asn": self.config.spine_asn,
                "leaf_asn_range": f"{self.config.leaf_asn_start}-{self.config.leaf_asn_start + self.config.num_leafs - 1}",
                "ecmp": True,
                "bfd": True,
            },
        }


def generate_topology(
    scale: str = "xs",
    output_dir: str | None = None,
    name: str | None = None,
    mgmt_subnet: str | None = None,
    mgmt_network: str | None = None,
    collector_ip: str | None = None,
) -> dict:
    """
    Convenience function to generate a topology of specified scale.

    Args:
        scale: Topology scale ('xs', 'small', 'medium', 'large')
        output_dir: Output directory path

    Returns:
        Dictionary with generated file paths and metadata
    """
    if scale not in TOPOLOGY_SCALES:
        raise ValueError(f"Unknown scale: {scale}. Available: {list(TOPOLOGY_SCALES.keys())}")

    base_config = TOPOLOGY_SCALES[scale]
    config = TopologyConfig(
        name=name or base_config.name,
        num_spines=base_config.num_spines,
        num_leafs=base_config.num_leafs,
        clients_per_leaf=base_config.clients_per_leaf,
        nos_kind=base_config.nos_kind,
        nos_image=base_config.nos_image,
        client_image=base_config.client_image,
        mgmt_ipv4_subnet=mgmt_subnet or base_config.mgmt_ipv4_subnet,
        mgmt_network_name=mgmt_network or base_config.mgmt_network_name,
        collector_ip=collector_ip or base_config.collector_ip,
        spine_asn=base_config.spine_asn,
        leaf_asn_start=base_config.leaf_asn_start,
    )
    generator = TopologyGenerator(config, output_dir)
    return generator.generate()
