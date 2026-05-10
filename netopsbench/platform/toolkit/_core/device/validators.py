"""Internal validation and resolution helpers for AgentToolkit."""

from __future__ import annotations

import ipaddress
import re
import subprocess

from netopsbench.platform.utils.proc import safe_run, sudo_prefix

DEVICE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
INTERFACE_PATTERN = re.compile(r"^[a-zA-Z0-9./-]{1,64}$")


def validate_device_name(device: str, field_name: str = "device") -> str:
    if not isinstance(device, str) or not DEVICE_PATTERN.fullmatch(device):
        raise ValueError(f"Invalid {field_name}: {device!r}")
    return device


def validate_interface_name(interface: str) -> str:
    if not isinstance(interface, str) or not INTERFACE_PATTERN.fullmatch(interface):
        raise ValueError(f"Invalid interface name: {interface!r}")
    return interface


def validate_ip_address(ip_value: str, field_name: str = "ip") -> str:
    try:
        return str(ipaddress.ip_address(ip_value))
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {ip_value!r}") from exc


def validate_prefix(prefix: str) -> str:
    try:
        return str(ipaddress.ip_network(prefix, strict=False))
    except ValueError as exc:
        raise ValueError(f"Invalid prefix: {prefix!r}") from exc


def resolve_container(toolkit, device: str, field_name: str = "device") -> str:
    safe_device = validate_device_name(device, field_name)
    container = toolkit.container_names.get(safe_device)
    if not container:
        raise ValueError(f"Unknown {field_name}: {safe_device}")
    return container


def get_client_device_names(toolkit) -> list[str]:
    if getattr(toolkit, "topology_metadata", None):
        clients = [
            str(client.get("name"))
            for client in toolkit.topology_metadata.get("devices", {}).get("clients", [])
            if client.get("name")
        ]
        if clients:
            return sorted(clients)
    return sorted(name for name in toolkit.container_names if str(name).lower().startswith("client"))


def require_client_source(toolkit, src: str, tool_name: str) -> str:
    safe_src = validate_device_name(src, field_name="source")
    client_names = get_client_device_names(toolkit)
    if safe_src not in client_names:
        available = ", ".join(client_names) if client_names else "no clients discovered"
        raise ValueError(
            f"{tool_name} only supports client sources to avoid source-address ambiguity on infrastructure "
            f"devices. Received source '{safe_src}'. Choose one of the discovered clients: {available}."
        )
    return safe_src


def docker_exec(container: str, cmd_args: list[str], timeout: int) -> subprocess.CompletedProcess:
    args = [*sudo_prefix(), "docker", "exec", container] + [str(arg) for arg in cmd_args]
    return safe_run(args, capture_output=True, text=True, timeout=timeout, check=False)
