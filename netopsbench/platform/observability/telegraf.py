#!/usr/bin/env python3
"""
Dynamic Telegraf Configuration Generator
Generates telegraf.conf from template based on topology metadata
"""

import json
import re
from importlib.resources import files
from pathlib import Path

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.models.topology import Device, DeviceRole, TopologyManifest
from netopsbench.platform.topology.configdb_payload import interface_names_from_configdb
from netopsbench.platform.topology.topology_utils import load_topology_manifest

logger = get_logger(__name__)

GNMI_PORT = 50051
GNMI_USERNAME = "admin"
GNMI_PASSWORD = ""
GNMI_ENCODING = "json_ietf"
GNMI_TARGET = "COUNTERS_DB"
GNMI_SUBSCRIPTION_MODE = "sample"
GNMI_SAMPLE_INTERVAL = "10s"
INTERNAL_INFLUXDB_URL = "http://influxdb:8086"
GNMI_INTERFACE_COUNTER_FIELDS = (
    "SAI_PORT_STAT_IF_IN_OCTETS",
    "SAI_PORT_STAT_IF_OUT_OCTETS",
    "SAI_PORT_STAT_IF_IN_UCAST_PKTS",
    "SAI_PORT_STAT_IF_OUT_UCAST_PKTS",
    "SAI_PORT_STAT_IF_IN_DISCARDS",
    "SAI_PORT_STAT_IF_OUT_DISCARDS",
    "SAI_PORT_STAT_IF_IN_ERRORS",
    "SAI_PORT_STAT_IF_OUT_ERRORS",
)


def _port_key(name: str) -> int:
    return int(name.replace("Ethernet", "")) if name.startswith("Ethernet") else 0


def _device_config_interfaces(topology_dir: Path, device_name: str) -> list[str]:
    config_path = topology_dir / "configs" / "sonic" / device_name / "config_db.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"Generated ConfigDB artifact not found: {config_path}")
    interfaces = interface_names_from_configdb(config_path)
    if not interfaces:
        raise ValueError(f"Generated ConfigDB artifact has no interfaces: {config_path}")
    return interfaces


def _role_port_names(topology_dir: Path, devices: list[dict]) -> list[str]:
    port_names: set[str] = set()
    for device in devices:
        port_names.update(_device_config_interfaces(topology_dir, str(device.get("name", ""))))
    return sorted(port_names, key=_port_key)


def _devices_for_role(raw_devices: list[Device], gnmi_port: int) -> list[dict]:
    devices = []
    for device in raw_devices or []:
        if not device.mgmt_ip:
            continue
        devices.append(
            {
                "name": device.name,
                "mgmt_ip": str(device.mgmt_ip).split("/")[0],
                "gnmi_port": gnmi_port,
            }
        )
    return devices


def _gnmi_addresses(devices: list[dict]) -> list[str]:
    return [f'"{device["mgmt_ip"]}:{device["gnmi_port"]}"' for device in devices]


def _gnmi_role_groups(manifest: TopologyManifest, gnmi_port: int) -> list[tuple[str, list[dict]]]:
    if manifest.family == "fat-tree":
        return [
            ("core", _devices_for_role(manifest.devices_by_role(DeviceRole.CORE), gnmi_port)),
            ("agg", _devices_for_role(manifest.devices_by_role(DeviceRole.AGG), gnmi_port)),
            ("edge", _devices_for_role(manifest.devices_by_role(DeviceRole.EDGE), gnmi_port)),
        ]

    return [
        ("spine", _devices_for_role(manifest.devices_by_role(DeviceRole.SPINE), gnmi_port)),
        ("leaf", _devices_for_role(manifest.devices_by_role(DeviceRole.LEAF), gnmi_port)),
    ]


def _render_gnmi_subscriptions(port_names: list[str]) -> str:
    subscriptions = []
    for port in port_names:
        subscription_lines = [
            "  [[inputs.gnmi.subscription]]",
            '    name = "interfaces"',
            f'    path = "COUNTERS/{port}"',
            f'    subscription_mode = "{GNMI_SUBSCRIPTION_MODE}"',
            f'    sample_interval = "{GNMI_SAMPLE_INTERVAL}"',
        ]
        subscriptions.append("\n".join(subscription_lines) + "\n")
    return "\n".join(subscriptions)


def _render_gnmi_input(
    role: str,
    devices: list[dict],
    port_names: list[str],
) -> str:
    if not devices or not port_names:
        return ""
    addresses = ",\n       ".join(_gnmi_addresses(devices))
    fieldpass = ", ".join(f'"{field}"' for field in GNMI_INTERFACE_COUNTER_FIELDS)
    subscriptions = _render_gnmi_subscriptions(port_names)
    return f"""# gNMI role: {role}
[[inputs.gnmi]]
  addresses = [
       {addresses}
  ]
  username = "{GNMI_USERNAME}"
  password = "{GNMI_PASSWORD}"
  encoding = "{GNMI_ENCODING}"
  redial = "10s"
  path_guessing_strategy = "subscription"
  tls_enable = false
  insecure_skip_verify = true
  target = "{GNMI_TARGET}"
  fieldpass = [{fieldpass}]

{subscriptions}"""


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
    - {{GNMI_INPUTS}}: Role-scoped SONiC gNMI inputs and subscriptions
    - {{IP_MAPPINGS}}: Starlark dictionary to map IPs to hostnames
    """
    # Read topology metadata
    topology_path = Path(topology_file)
    if not topology_path.exists():
        raise FileNotFoundError(f"Topology file not found: {topology_file}")

    manifest = load_topology_manifest(topology_path)

    resolved_influxdb_url = influxdb_url or INTERNAL_INFLUXDB_URL
    resolved_influxdb_token = influxdb_token or config.influxdb_token
    resolved_influxdb_org = influxdb_org or config.influxdb_org
    resolved_influxdb_bucket = influxdb_bucket or config.influxdb_bucket
    resolved_topology_id = topology_id or manifest.topology_id

    topology_dir = topology_path.parent
    role_groups = _gnmi_role_groups(manifest, GNMI_PORT)
    devices = [device for _role, role_devices in role_groups for device in role_devices]

    logger.info("Found %d network devices:", len(devices))
    for d in devices:
        logger.debug("  - %s: %s", d["name"], d["mgmt_ip"])

    rendered_inputs = []
    role_subscription_counts: dict[str, set[int]] = {}
    for role, role_devices in role_groups:
        port_names = _role_port_names(topology_dir, role_devices)
        role_subscription_counts[role] = {len(port_names)}
        rendered_inputs.append(
            _render_gnmi_input(
                role,
                role_devices,
                port_names,
            ),
        )
    gnmi_inputs_str = "\n".join(block for block in rendered_inputs if block)

    # Generate one O(1) lookup shared by all supported source-like tags.
    ip_mappings = {str(device["mgmt_ip"]): str(device["name"]) for device in devices}
    ip_mappings_str = json.dumps(ip_mappings, indent=2, sort_keys=True)

    # Read template file
    template_resource = files("netopsbench.platform.observability").joinpath("assets", "telegraf.conf.template")
    if not template_resource.is_file():
        raise FileNotFoundError("Packaged Telegraf template is missing")
    template = template_resource.read_text(encoding="utf-8")

    # Replace placeholders
    rendered_config = template.replace("{{GNMI_INPUTS}}", gnmi_inputs_str)
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
    output_path = Path(output_file) if output_file else (Path.cwd() / "observability" / "telegraf.conf")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered_config)

    logger.info("Telegraf configuration updated: %s", output_path)
    logger.info("  - %d devices configured", len(devices))
    logger.info("  - gNMI targets: %d", len(devices))
    for role, counts in role_subscription_counts.items():
        logger.info("  - gNMI %s subscriptions per target: %s", role, ", ".join(str(item) for item in sorted(counts)))
    logger.info("  - gNMI subscription mode: %s", GNMI_SUBSCRIPTION_MODE)
    logger.info("  - IP mappings: %d", len(ip_mappings))
    logger.info("  - InfluxDB bucket: %s", resolved_influxdb_bucket)
    return 0
