"""
Topology Metadata Generator - Parse existing Containerlab YAML and generate metadata.

This utility creates topology.json metadata files from existing Containerlab YAML files.
Useful for pre-existing topologies that don't have metadata.
"""

import json
from pathlib import Path

import yaml

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


def parse_clab_yaml(yaml_path: str) -> dict:
    """
    Parse a Containerlab YAML file and extract topology metadata.

    Args:
        yaml_path: Path to the .clab.yaml file

    Returns:
        Dictionary containing topology metadata
    """
    yaml_file = Path(yaml_path)
    if not yaml_file.exists():
        raise FileNotFoundError(f"YAML file not found: {yaml_path}")

    with open(yaml_file) as f:
        clab_config = yaml.safe_load(f)

    topology = clab_config.get("topology", {})
    nodes = topology.get("nodes", {})
    links = topology.get("links", [])

    # Extract devices by type
    devices = {"spines": [], "leafs": [], "clients": []}

    # Parse nodes
    for node_name, node_config in nodes.items():
        group = node_config.get("group", "")
        mgmt_ip = node_config.get("mgmt-ipv4", "")

        if group == "spine":
            # Extract spine info from configs or use defaults
            devices["spines"].append(
                {
                    "name": node_name,
                    "mgmt_ip": mgmt_ip,
                    "router_id": f"10.0.0.{len(devices['spines']) + 1}",
                    "asn": 65001,  # Default spine ASN
                }
            )
        elif group == "leaf":
            leaf_idx = len(devices["leafs"]) + 1
            devices["leafs"].append(
                {
                    "name": node_name,
                    "mgmt_ip": mgmt_ip,
                    "router_id": f"10.0.0.{10 + leaf_idx}",
                    "asn": 65010 + leaf_idx,
                    "client_subnet": f"192.168.{leaf_idx}.0/24",
                }
            )
        elif group == "client":
            # Parse client IP from exec commands
            data_ip = None
            leaf_name = None

            exec_commands = node_config.get("exec", [])
            for cmd in exec_commands:
                if "ip addr add" in cmd:
                    # Extract IP from command like "ip addr add 192.168.1.2/24 dev eth1"
                    parts = cmd.split()
                    if "add" in parts:
                        ip_idx = parts.index("add") + 1
                        if ip_idx < len(parts):
                            data_ip = parts[ip_idx].split("/")[0]

            # Determine which leaf this client connects to from links
            for link in links:
                endpoints = link.get("endpoints", [])
                for endpoint in endpoints:
                    if node_name in endpoint:
                        # Find the other endpoint (should be a leaf)
                        other_endpoint = [e for e in endpoints if node_name not in e][0]
                        leaf_name = other_endpoint.split(":")[0]
                        break

            # Determine rack from leaf
            rack = None
            if leaf_name:
                # Extract leaf number
                leaf_num = "".join(filter(str.isdigit, leaf_name))
                rack = f"rack{leaf_num}"

            devices["clients"].append(
                {
                    "name": node_name,
                    "mgmt_ip": mgmt_ip,
                    "data_ip": data_ip or "unknown",
                    "leaf": leaf_name or "unknown",
                    "rack": rack or "unknown",
                }
            )

    # Parse links
    parsed_links = []
    for link in links:
        endpoints = link.get("endpoints", [])
        if len(endpoints) >= 2:
            endpoint_names = [e.split(":")[0] for e in endpoints]

            # Determine link type
            link_type = "unknown"
            if any("spine" in e for e in endpoint_names) and any("leaf" in e for e in endpoint_names):
                link_type = "spine-leaf"
            elif any("leaf" in e for e in endpoint_names) and any("client" in e for e in endpoint_names):
                link_type = "leaf-client"

            parsed_links.append({"endpoints": endpoint_names, "type": link_type})

    # Calculate scale
    num_spines = len(devices["spines"])
    num_leafs = len(devices["leafs"])
    num_clients = len(devices["clients"])
    clients_per_leaf = num_clients // num_leafs if num_leafs > 0 else 0

    # Generate metadata
    metadata = {
        "name": clab_config.get("name", "dcn"),
        "scale": {
            "num_spines": num_spines,
            "num_leafs": num_leafs,
            "clients_per_leaf": clients_per_leaf,
            "total_clients": num_clients,
            "total_devices": num_spines + num_leafs,
        },
        "devices": devices,
        "links": parsed_links,
        "routing": {
            "protocol": "BGP",
            "spine_asn": 65001,
            "leaf_asn_range": f"65011-{65010 + num_leafs}",
            "ecmp": True,
            "bfd": True,
        },
    }

    return metadata


def generate_metadata_file(yaml_path: str, output_path: str = None) -> str:
    """
    Generate topology.json metadata file from Containerlab YAML.

    Args:
        yaml_path: Path to the .clab.yaml file
        output_path: Optional output path for topology.json
                    (defaults to same directory as YAML)

    Returns:
        Path to the generated topology.json file
    """
    yaml_file = Path(yaml_path)

    if output_path is None:
        output_path = yaml_file.parent / "topology.json"
    else:
        output_path = Path(output_path)

    # Parse YAML and generate metadata
    metadata = parse_clab_yaml(yaml_path)

    # Write metadata file
    with open(output_path, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info("Generated topology metadata: %s", output_path)
    logger.info("  - Spines: %s", metadata["scale"]["num_spines"])
    logger.info("  - Leafs: %s", metadata["scale"]["num_leafs"])
    logger.info("  - Clients: %s", metadata["scale"]["total_clients"])

    return str(output_path)
