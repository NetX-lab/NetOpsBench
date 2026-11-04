"""Interface observations without simulator traffic-control state."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime

from netopsbench.platform.topology.topology_utils import is_network_device_name
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent

from ..common import ToolResult
from .interface_parsers import merge_interface_tables, parse_ip_link_stats
from .text_parsers import parse_table


def _link_observations(text: str, interface: str | None) -> str:
    """Select complete ip-link records and omit the qdisc implementation field."""
    blocks = re.split(r"(?m)(?=^\d+: )", text)
    selected = []
    for block in blocks:
        match = re.match(r"^\d+:\s+([^:@]+)(?:@[^:]+)?:", block)
        if not match or (interface and not are_interfaces_equivalent(match[1], interface)):
            continue
        lines = block.splitlines(keepends=True)
        lines[0] = re.sub(r"\s+qdisc\s+\S+", "", lines[0])
        selected.append("".join(lines))
    return "".join(selected)


def _interface_table(text: str, interface: str | None) -> str:
    if not interface:
        return text
    # Keep table headings/separators, selecting rows by their first column.
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.strip()
        or line.lstrip().startswith(("Interface", "IFACE", "Port", "---"))
        or are_interfaces_equivalent(line.split()[0], interface)
    )


class InterfaceOpsMixin:
    def get_device_interfaces(
        self, device: str, format: str = "structured", interface: str | None = None
    ) -> ToolResult:
        try:
            self._validate_device_name(device)
            if interface is not None:
                self._validate_interface_name(interface)
            view = (format or "raw").lower()
            if view not in {"raw", "structured", "summary", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            container = self._resolve_container(device)
            raw = None
            if is_network_device_name(device):
                brief = self._docker_exec(container, ["bash", "-lc", "show interfaces status"], timeout=30)
                detail = self._docker_exec(container, ["bash", "-lc", "show interfaces counters"], timeout=30)
                if brief.returncode == 0 or detail.returncode == 0:
                    brief_text = _interface_table(brief.stdout if brief.returncode == 0 else "", interface)
                    detail_text = _interface_table(detail.stdout if detail.returncode == 0 else "", interface)
                    raw = {"device": device, "brief": brief_text, "detail": detail_text}
                    rows = merge_interface_tables(parse_table(brief_text), parse_table(detail_text))
            # SONiC's port tables omit real non-port interfaces such as lo and
            # management interfaces. Check Linux before declaring one absent.
            if raw is None or (interface and not rows):
                result = self._docker_exec(container, ["ip", "-s", "link", "show"], timeout=30)
                if result.returncode:
                    return ToolResult(success=False, data=None, error="Interface state unavailable")
                text = _link_observations(result.stdout, interface)
                rows = parse_ip_link_stats(text)
                raw = {"device": device, "interfaces": text}
            if interface and not rows:
                return ToolResult(success=False, data=None, error="Interface not found in collected device state")
            structured = {"device": device, "interfaces": rows}
            if not rows:
                structured["warning"] = "Unable to parse interface tables; check raw output."
            data = raw if view == "raw" else structured
            if view == "summary":
                data = self._summarize_interface_data(device, rows)
            elif view == "both":
                data = {"device": device, "raw": raw, "structured": structured}
            addresses = self._docker_exec(container, ["ip", "-j", "-4", "addr", "show"], timeout=30)
            if addresses.returncode:
                data["address_collection_error"] = "Address collection failed"
            else:
                data["addresses"] = {
                    row["ifname"]: [f"{a['local']}/{a['prefixlen']}" for a in row.get("addr_info", [])]
                    for row in json.loads(addresses.stdout)
                    if not interface or are_interfaces_equivalent(row["ifname"], interface)
                }
            data["collected_at"] = datetime.now(UTC).isoformat()
            return ToolResult(success=True, data=data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except ValueError:
            return ToolResult(success=False, data=None, error="Invalid interface request or device response")
        except Exception:
            return ToolResult(success=False, data=None, error="Interface collection failed")

    @staticmethod
    def _summarize_interface_data(device: str, interfaces: list[dict]) -> dict:
        status_keys = {"admin", "oper", "status", "protocol", "state", "speed", "mtu"}
        signal_fragments = ("error", "err", "discard", "drop", "drp", "crc", "fcs", "carrier", "coll")
        summarized = []
        for entry in interfaces:
            if not isinstance(entry, dict):
                continue
            item = {"name": entry.get("name")}
            for key, value in entry.items():
                if key == "name":
                    continue
                key_text = str(key).lower()
                include = key in status_keys or any(fragment in key_text for fragment in signal_fragments)
                if include:
                    item[key] = value
            summarized.append({key: value for key, value in item.items() if value not in (None, "")})
        return {
            "device": device,
            "view": "summary",
            "interface_count": len(interfaces),
            "interfaces": summarized,
        }
