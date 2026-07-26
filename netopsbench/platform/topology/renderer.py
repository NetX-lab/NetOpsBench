"""Shared renderer for CLOS and fat-tree fabric plans."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from netopsbench.platform.client_agent.config import (
    CLIENT_AGENT_CONFIG_NAME,
    write_client_agent_config,
)

from .config import (
    SONIC_BASE_CONFIG_DB,
    SONIC_HWSKU,
    SONIC_LANEMAP_PATH,
    SONIC_PLATFORM,
    SONIC_PORT_CONFIG_PATH,
    SONIC_START_WRAPPER_SOURCE,
)
from .plan import DevicePlan, FabricPlan


def _sonic_port_attrs(port_idx: int) -> dict[str, str]:
    eth = port_idx * 4
    first_lane = eth + 1
    lanes = ",".join(str(first_lane + offset) for offset in range(4))
    return {
        "alias": f"fortyGigE0/{eth}",
        "index": str(port_idx),
        "lanes": lanes,
        "speed": "100000",
        "subport": "0",
        "admin_status": "up",
    }


def _port_entries(required_ports: int) -> dict[str, dict[str, str]]:
    return {f"Ethernet{port_idx * 4}": _sonic_port_attrs(port_idx) for port_idx in range(required_ports)}


def _port_config_ini(required_ports: int) -> str:
    lines = ["# name          lanes             alias             index       speed"]
    for port_idx in range(required_ports):
        name = f"Ethernet{port_idx * 4}"
        attrs = _sonic_port_attrs(port_idx)
        lines.append(f"{name:<15} {attrs['lanes']:<17} {attrs['alias']:<17} {attrs['index']:<11} {attrs['speed']}")
    return "\n".join(lines) + "\n"


def _lanemap_ini(required_ports: int) -> str:
    lines = []
    for port_idx in range(required_ports):
        attrs = _sonic_port_attrs(port_idx)
        lines.append(f"eth{port_idx + 1}:{attrs['lanes']}")
    return "\n".join(lines) + "\n"


def _device_mac(device: str) -> str:
    digest = hashlib.sha256(device.encode("utf-8")).digest()
    return "02:" + ":".join(f"{byte:02x}" for byte in digest[:5])


def _load_base_config_db() -> dict[str, Any]:
    with SONIC_BASE_CONFIG_DB.open(encoding="utf-8") as handle:
        return deepcopy(json.load(handle))


def _interface_sort_key(name: str) -> int:
    return int(name[8:]) if name.startswith("Ethernet") else 0


def _build_config_db(plan: FabricPlan, device_plan: DevicePlan) -> dict[str, Any]:
    config_db = _load_base_config_db()
    metadata = config_db.setdefault("DEVICE_METADATA", {}).setdefault("localhost", {})
    metadata["hostname"] = device_plan.name
    metadata["hwsku"] = SONIC_HWSKU
    metadata["platform"] = SONIC_PLATFORM
    metadata["mac"] = _device_mac(device_plan.name)

    ports = _port_entries(device_plan.required_ports)
    config_db["PORT"] = ports
    config_db["CABLE_LENGTH"] = {"AZURE": {name: "0m" for name in ports}}
    config_db["BREAKOUT_CFG"] = {name: {"brkout_mode": "1x100G[40G]"} for name in ports}
    config_db["SYSLOG_SERVER"] = {"telegraf": {"server": plan.render_settings.syslog_collector}}

    interface_table: dict[str, dict[str, str]] = {}
    for interface_name in sorted(device_plan.configdb_interface_cidrs, key=_interface_sort_key):
        interface_table[interface_name] = {}
        for cidr in device_plan.configdb_interface_cidrs[interface_name]:
            interface_table[f"{interface_name}|{cidr}"] = {}
    config_db["INTERFACE"] = interface_table

    return config_db


def _frr_config(device_plan: DevicePlan) -> str:
    lines = [
        "frr version 10.3",
        "frr defaults traditional",
        f"hostname {device_plan.name}",
        "log syslog informational",
        "no ipv6 forwarding",
        "service integrated-vtysh-config",
        "!",
        "route-map RM-ALLOW permit 10",
        "!",
        f"router bgp {device_plan.bgp_asn}",
        f" bgp router-id {device_plan.bgp_router_id}",
        " no bgp ebgp-requires-policy",
        " bgp bestpath as-path multipath-relax",
    ]
    for neighbor in device_plan.bgp_neighbors:
        lines.append(f" neighbor {neighbor.peer_ip} remote-as {neighbor.remote_as}")
    lines.extend([" !", " address-family ipv4 unicast"])
    lines.append("  maximum-paths 64")
    for neighbor in device_plan.bgp_neighbors:
        lines.append(f"  neighbor {neighbor.peer_ip} activate")
        lines.append(f"  neighbor {neighbor.peer_ip} route-map RM-ALLOW out")
    for prefix in device_plan.bgp_networks:
        lines.append(f"  network {prefix} route-map RM-ALLOW")
    lines.extend([" exit-address-family", "!", "line vty", "!"])
    return "\n".join(lines) + "\n"


def _containerlab_topology(plan: FabricPlan) -> dict[str, Any]:
    topology: dict[str, Any] = {
        "name": plan.manifest.name,
        "mgmt": {
            "network": plan.manifest.management.network,
            "ipv4-subnet": plan.manifest.management.ipv4_subnet,
        },
        "topology": {
            "kinds": {
                plan.nos_kind: {
                    "image": plan.nos_image,
                    "binds": [
                        "configs/sonic/__clabNodeName__/config_db.json:/etc/sonic/config_db.json:rw",
                        f"configs/sonic/__clabNodeName__/port_config.ini:{SONIC_PORT_CONFIG_PATH}:rw",
                        f"configs/sonic/__clabNodeName__/lanemap.ini:{SONIC_LANEMAP_PATH}:rw",
                        "configs/sonic/start.sh:/usr/bin/start.sh:ro",
                        "configs/frr/__clabNodeName__.conf:/etc/frr/frr.conf:rw",
                    ],
                },
                "linux": {
                    "image": plan.client_image,
                    "binds": ["configs/client-agent:/etc/netopsbench:ro"],
                },
            },
            "nodes": {},
            "links": [],
        },
    }
    nodes = topology["topology"]["nodes"]
    for device_plan in plan.device_plans:
        device = device_plan.device
        if device_plan.is_client:
            nodes[device.name] = {
                "kind": "linux",
                "group": "client",
                "mgmt-ipv4": device.mgmt_ip,
                "exec": list(device_plan.client_commands),
            }
        else:
            nodes[device.name] = {
                "kind": plan.nos_kind,
                "group": device.role.value,
                "mgmt-ipv4": device.mgmt_ip,
            }
    topology["topology"]["links"] = [
        {
            "endpoints": [f"{endpoint.device}:{endpoint.interface}" for endpoint in link.endpoints],
            "mtu": link.mtu,
        }
        for link in plan.manifest.links
    ]
    return topology


def _render_into(plan: FabricPlan, root: Path) -> dict[str, Any]:
    sonic_root = root / "configs" / "sonic"
    frr_root = root / "configs" / "frr"
    client_agent_root = root / "configs" / "client-agent"
    sonic_root.mkdir(parents=True, exist_ok=True)
    frr_root.mkdir(parents=True, exist_ok=True)
    client_agent_root.mkdir(parents=True, exist_ok=True)

    sonic_start_wrapper = sonic_root / "start.sh"
    if not SONIC_START_WRAPPER_SOURCE.is_file():
        raise FileNotFoundError(f"SONiC startup wrapper source not found: {SONIC_START_WRAPPER_SOURCE}")
    sonic_start_wrapper.write_bytes(SONIC_START_WRAPPER_SOURCE.read_bytes())
    sonic_start_wrapper.chmod(0o755)

    yaml_path = root / f"{plan.manifest.name}.clab.yaml"
    with yaml_path.open("w", encoding="utf-8") as handle:
        handle.write(plan.yaml_header)
        yaml.dump(_containerlab_topology(plan), handle, default_flow_style=False, sort_keys=False)

    config_paths: list[str] = []
    frr_paths: list[str] = []
    for device_plan in plan.device_plans:
        if device_plan.is_client:
            continue
        sonic_dir = sonic_root / device_plan.name
        sonic_dir.mkdir(parents=True, exist_ok=True)
        config_db_path = sonic_dir / "config_db.json"
        config_db_path.write_text(
            json.dumps(_build_config_db(plan, device_plan), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (sonic_dir / "port_config.ini").write_text(
            _port_config_ini(device_plan.required_ports),
            encoding="utf-8",
        )
        (sonic_dir / "lanemap.ini").write_text(_lanemap_ini(device_plan.required_ports), encoding="utf-8")
        frr_path = frr_root / f"{device_plan.name}.conf"
        frr_path.write_text(_frr_config(device_plan), encoding="utf-8")
        config_paths.append(str(config_db_path))
        frr_paths.append(str(frr_path))

    metadata_path = root / "topology.json"
    metadata_path.write_text(
        json.dumps(plan.manifest.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    client_agent_config = write_client_agent_config(
        plan.manifest,
        client_agent_root / CLIENT_AGENT_CONFIG_NAME,
    )
    return {
        "yaml_file": str(yaml_path),
        "metadata_file": str(metadata_path),
        "client_agent_config_file": str(client_agent_config),
        "config_files": config_paths,
        "sonic_start_wrapper_file": str(sonic_start_wrapper),
        "frr_config_files": frr_paths,
        "metadata": plan.manifest.model_dump(mode="json"),
        "agent_topology": plan.manifest.to_agent_topology(),
    }


def render_fabric_plan(plan: FabricPlan, output_dir: str | Path) -> dict[str, Any]:
    """Atomically replace every artifact for one canonical fabric plan."""
    root = Path(output_dir)
    root.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{root.name}.staging-", dir=root.parent))
    backup = root.with_name(f".{root.name}.backup")
    try:
        result = _render_into(plan, staged)
        if backup.exists():
            shutil.rmtree(backup)
        if root.exists():
            os.replace(root, backup)
        try:
            os.replace(staged, root)
        except Exception:
            if backup.exists() and not root.exists():
                os.replace(backup, root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    staged_prefix = str(staged)
    for key in (
        "yaml_file",
        "metadata_file",
        "client_agent_config_file",
        "sonic_start_wrapper_file",
    ):
        result[key] = str(root) + str(result[key])[len(staged_prefix) :]
    for key in ("config_files", "frr_config_files"):
        result[key] = [str(root) + str(path)[len(staged_prefix) :] for path in result[key]]
    return result


__all__ = ["render_fabric_plan"]
