#!/usr/bin/env python3
"""
Dynamic Telegraf Configuration Generator
Generates telegraf.conf from template based on topology metadata
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from netopsbench.config import config, repo_root
from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)

REPO_ROOT = repo_root()


def update_telegraf_config(
    topology_file: str,
    output_file: str | None = None,
    influxdb_url: str | None = None,
    influxdb_token: str | None = None,
    influxdb_org: str | None = None,
    influxdb_bucket: str | None = None,
    topology_id: str | None = None,
):
    """
    Generate telegraf.conf from template using topology metadata.

    Args:
        topology_file: Path to topology.json file
        output_file: Optional output path for generated telegraf.conf
        influxdb_url: Optional InfluxDB URL override
        influxdb_token: Optional InfluxDB token override
        influxdb_org: Optional InfluxDB organization override
        influxdb_bucket: Optional InfluxDB bucket override
        topology_id: Optional topology identifier tag override

    Generates telegraf.conf with:
    - {{GNMI_ADDRESSES}}: List of device management IPs for gNMI polling
    - {{IP_MAPPINGS}}: Processor rules to map IPs to hostnames
    """
    # Read topology metadata
    topology_path = Path(topology_file)
    if not topology_path.exists():
        logger.error("Topology file not found: %s", topology_file)
        sys.exit(1)

    with open(topology_path) as f:
        topo = json.load(f)

    # gNMI settings (override with env vars if needed)
    gnmi_port = int(os.getenv("SONIC_GNMI_PORT", os.getenv("GNMI_PORT", "50051")))
    gnmi_username = os.getenv("SONIC_GNMI_USERNAME", os.getenv("GNMI_USERNAME", "admin"))
    gnmi_password = os.getenv("SONIC_GNMI_PASSWORD", os.getenv("GNMI_PASSWORD", ""))
    if not gnmi_password:
        logger.warning("SONIC_GNMI_PASSWORD is not set; gNMI telemetry collection will likely fail.")
    gnmi_encoding = os.getenv("SONIC_GNMI_ENCODING", os.getenv("GNMI_ENCODING", "json_ietf"))
    gnmi_target = os.getenv("SONIC_GNMI_TARGET", os.getenv("GNMI_TARGET", "COUNTERS_DB"))
    gnmi_subscription_mode = (
        os.getenv(
            "SONIC_GNMI_SUBSCRIPTION_MODE",
            os.getenv("GNMI_SUBSCRIPTION_MODE", "on_change"),
        )
        .strip()
        .lower()
    )
    if gnmi_subscription_mode not in {"on_change", "sample", "target_defined"}:
        logger.error(
            "unsupported gNMI subscription mode %r; expected one of on_change, sample, target_defined",
            gnmi_subscription_mode,
        )
        sys.exit(1)
    gnmi_sample_interval = os.getenv("SONIC_GNMI_SAMPLE_INTERVAL", os.getenv("GNMI_SAMPLE_INTERVAL", "10s")).strip()
    resolved_influxdb_url = influxdb_url or os.getenv("NETOPSBENCH_INFLUXDB_URL", "http://influxdb:8086")
    resolved_influxdb_token = influxdb_token or os.getenv(
        "NETOPSBENCH_INFLUXDB_TOKEN",
        config.influxdb_token,
    )
    resolved_influxdb_org = influxdb_org or os.getenv("NETOPSBENCH_INFLUXDB_ORG", config.influxdb_org)
    resolved_influxdb_bucket = influxdb_bucket or os.getenv("NETOPSBENCH_INFLUXDB_BUCKET", config.influxdb_bucket)
    resolved_topology_id = (
        topology_id
        or os.getenv("NETOPSBENCH_TOPOLOGY_ID")
        or str(topo.get("name") or "").strip()
        or topology_path.parent.name
    )

    # sFlow settings (override with env vars if needed)
    sflow_service_address = os.getenv("SONIC_SFLOW_SERVICE_ADDRESS", os.getenv("SFLOW_SERVICE_ADDRESS", "udp://:6343"))
    if "://" not in sflow_service_address:
        sflow_service_address = f"udp://{sflow_service_address}"

    # Extract device information
    devices = []

    # Add spines
    for spine in topo["devices"]["spines"]:
        devices.append(
            {
                "name": spine["name"],
                "mgmt_ip": spine["mgmt_ip"].split("/")[0],  # Remove CIDR notation
                "gnmi_port": gnmi_port,
            }
        )

    # Add leafs
    for leaf in topo["devices"]["leafs"]:
        devices.append(
            {
                "name": leaf["name"],
                "mgmt_ip": leaf["mgmt_ip"].split("/")[0],  # Remove CIDR notation
                "gnmi_port": gnmi_port,
            }
        )

    logger.info("Found %d network devices:", len(devices))
    for d in devices:
        logger.info("  - %s: %s", d["name"], d["mgmt_ip"])

    # Derive SONiC front-panel ports used by this topology (Ethernet0/4/8...)
    scale = topo.get("scale", {})
    num_spines = int(scale.get("num_spines", 0))
    num_leafs = int(scale.get("num_leafs", 0))
    clients_per_leaf = int(scale.get("clients_per_leaf", 0))

    port_set = set()
    for leaf_idx in range(1, num_leafs + 1):
        port_set.add(f"Ethernet{(leaf_idx - 1) * 4}")
    for spine_idx in range(1, num_spines + 1):
        port_set.add(f"Ethernet{(spine_idx - 1) * 4}")
    for client_idx in range(1, clients_per_leaf + 1):
        port_set.add(f"Ethernet{(num_spines + client_idx - 1) * 4}")

    def _port_key(name: str) -> int:
        return int(name.replace("Ethernet", "")) if name.startswith("Ethernet") else 0

    port_names = sorted(port_set, key=_port_key)

    # Generate gNMI address list
    gnmi_addresses = [f'"{d["mgmt_ip"]}:{d["gnmi_port"]}"' for d in devices]
    gnmi_addresses_str = ",\n       ".join(gnmi_addresses)

    # Generate gNMI subscriptions (explicit per-port paths)
    gnmi_subscriptions = []
    for port in port_names:
        subscription_lines = [
            "  [[inputs.gnmi.subscription]]",
            '    name = "interfaces"',
            f'    path = "COUNTERS/{port}"',
            f'    subscription_mode = "{gnmi_subscription_mode}"',
        ]
        if gnmi_subscription_mode == "sample":
            subscription_lines.append(f'    sample_interval = "{gnmi_sample_interval}"')
        gnmi_subscriptions.append("\n".join(subscription_lines) + "\n")
    gnmi_subscriptions_str = "\n".join(gnmi_subscriptions)

    # Generate IP to hostname mappings (processor rules)
    ip_mappings = []
    mapping_keys = ["source", "agent_host", "agent_ip", "agent", "agent_address", "address", "target"]
    for key in mapping_keys:
        for d in devices:
            ip_mappings.append(f'''
  [[processors.regex.tags]]
    key = "{key}"
    pattern = "^{d['mgmt_ip']}$"
    replacement = "{d['name']}"''')
    ip_mappings_str = "".join(ip_mappings)

    # Read template file
    template_file = REPO_ROOT / "observability" / "telegraf.conf.template"

    if not template_file.exists():
        logger.error("Template file not found: %s", template_file)
        logger.error("Please create telegraf.conf.template first")
        sys.exit(1)

    with open(template_file, encoding="utf-8") as f:
        template = f.read()

    # Replace placeholders
    rendered_config = template.replace("{{GNMI_ADDRESSES}}", gnmi_addresses_str)
    rendered_config = rendered_config.replace("{{GNMI_USERNAME}}", gnmi_username)
    rendered_config = rendered_config.replace("{{GNMI_PASSWORD}}", gnmi_password)
    rendered_config = rendered_config.replace("{{GNMI_ENCODING}}", gnmi_encoding)
    rendered_config = rendered_config.replace("{{GNMI_TARGET}}", gnmi_target)
    rendered_config = rendered_config.replace("{{GNMI_SUBSCRIPTIONS}}", gnmi_subscriptions_str)
    rendered_config = rendered_config.replace("{{SFLOW_SERVICE_ADDRESS}}", sflow_service_address)
    rendered_config = rendered_config.replace("{{IP_MAPPINGS}}", ip_mappings_str)
    rendered_config = rendered_config.replace("{{INFLUXDB_URL}}", resolved_influxdb_url)
    rendered_config = rendered_config.replace("{{INFLUXDB_TOKEN}}", resolved_influxdb_token)
    rendered_config = rendered_config.replace("{{INFLUXDB_ORG}}", resolved_influxdb_org)
    rendered_config = rendered_config.replace("{{INFLUXDB_BUCKET}}", resolved_influxdb_bucket)
    rendered_config = rendered_config.replace("{{TOPOLOGY_ID}}", resolved_topology_id)

    # Validate all placeholders have been replaced
    unreplaced = re.findall(r"\{\{[A-Z_]+\}\}", rendered_config)
    if unreplaced:
        logger.warning(
            "Unreplaced template placeholders: %s",
            ", ".join(sorted(set(unreplaced))),
        )

    # Write output file
    output_path = Path(output_file) if output_file else (REPO_ROOT / "observability" / "telegraf.conf")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered_config)

    logger.info("Telegraf configuration updated: %s", output_path)
    logger.info("  - %d devices configured", len(devices))
    logger.info("  - gNMI targets: %d", len(gnmi_addresses))
    logger.info("  - gNMI subscription mode: %s", gnmi_subscription_mode)
    logger.info("  - IP mappings: %d", len(ip_mappings))
    logger.info("  - InfluxDB bucket: %s", resolved_influxdb_bucket)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate telegraf.conf from topology metadata")
    parser.add_argument("topology_file", help="Path to topology.json")
    parser.add_argument("--output", help="Output telegraf config path")
    parser.add_argument("--influxdb-url", help="InfluxDB URL override")
    parser.add_argument("--influxdb-token", help="InfluxDB token override")
    parser.add_argument("--influxdb-org", help="InfluxDB organization override")
    parser.add_argument("--bucket", help="InfluxDB bucket override")
    parser.add_argument("--topology-id", help="Topology identifier override")
    args = parser.parse_args()

    return update_telegraf_config(
        args.topology_file,
        output_file=args.output,
        influxdb_url=args.influxdb_url,
        influxdb_token=args.influxdb_token,
        influxdb_org=args.influxdb_org,
        influxdb_bucket=args.bucket,
        topology_id=args.topology_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
