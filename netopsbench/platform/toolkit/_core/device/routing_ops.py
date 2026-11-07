"""Routing-oriented device toolkit operations."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime

from netopsbench.logging_utils import get_logger
from netopsbench.platform.observability.bgp_parser import parse_bgp_summary
from netopsbench.platform.topology.topology_utils import is_network_device_name

from ..common import PublicArgumentError, ToolResult, truncate_text_lines
from .route_parsers import parse_route_table

logger = get_logger(__name__)


class RoutingOpsMixin:
    def get_device_status(self, device: str) -> ToolResult:
        """Read container, accessibility and supported service state without diagnosis labels."""
        import time

        from netopsbench.platform.utils.control import OperationControl
        from netopsbench.platform.utils.proc import docker_prefix, safe_run

        try:
            container = self._resolve_container(device)
            from ..control import current_tool_control

            parent = current_tool_control()
            deadline = time.monotonic() + 30
            control = OperationControl(deadline=min(deadline, parent.deadline) if parent else deadline)
            result = safe_run(
                [*docker_prefix(), "docker", "inspect", "--format", "{{json .State}}", container],
                timeout=control.remaining(15),
                control=control,
            )
            if result.returncode:
                return ToolResult(success=False, data=None, error=result.stderr or "Cannot inspect node")
            state = json.loads(result.stdout)
            data = {
                "device": device,
                "running": bool(state.get("Running")),
                "runtime_state": state.get("Status"),
                "accessible": False,
                "services": {},
                "collected_at": datetime.now(UTC).isoformat(),
            }
            if data["running"]:
                probe = self._docker_exec(container, ["true"], timeout=control.remaining(5))
                data["accessible"] = probe.returncode == 0
                if is_network_device_name(device) and data["accessible"]:
                    services = self._docker_exec(
                        container, ["supervisorctl", "status", "bgpd", "zebra"], timeout=control.remaining(10)
                    )
                    for line in services.stdout.splitlines():
                        fields = line.split()
                        if len(fields) >= 2 and fields[0] in {"bgpd", "zebra"}:
                            data["services"][fields[0]] = fields[1]
                    if not data["services"]:
                        data["service_collection_error"] = services.stderr or "Service states unavailable"
            return ToolResult(success=True, data=data)
        except Exception as exc:
            return ToolResult(success=False, data=None, error=str(exc))

    def get_bgp_neighbors(self, device: str, format: str = "structured") -> ToolResult:
        try:
            safe_format = (format or "raw").lower()
            if safe_format == "json":
                safe_format = "structured"
            if safe_format not in {"raw", "structured", "both"}:
                return ToolResult(success=False, data=None, error=PublicArgumentError.BGP_FORMAT)
            self._validate_device_name(device)
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} does not run BGP")
            container = self._resolve_container(device)
            result = self._docker_exec(container, ["vtysh", "-c", "show ip bgp summary"], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            raw_data = {"device": device, "bgp_neighbors": result.stdout}
            if safe_format in {"structured", "both"}:
                neighbors = parse_bgp_summary(result.stdout)
                # Enrich non-Established peers with detail (last error, reset reason)
                for nbr in neighbors:
                    if nbr.get("state") and nbr["state"] != "Established" and nbr.get("neighbor"):
                        detail = self._get_bgp_neighbor_detail(container, nbr["neighbor"])
                        if detail:
                            nbr["detail"] = detail
                structured = {"device": device, "neighbors": neighbors}
                if not structured["neighbors"]:
                    structured["warning"] = "Unable to parse BGP summary; check raw output."
                if safe_format == "structured":
                    return ToolResult(success=True, data=structured)
                return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
            return ToolResult(success=True, data=raw_data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_bgp_neighbor(self, device: str, peer: str, view: str = "detail", prefix: str | None = None) -> ToolResult:
        """Return live detail for one BGP session on one device."""
        if view in {"advertised-routes", "advertised"}:
            view = "advertised_routes"
        if view not in {"detail", "advertised_routes"}:
            return ToolResult(success=False, data=None, error=PublicArgumentError.BGP_VIEW)
        try:
            self._validate_device_name(device)
            safe_peer = self._validate_ip_address(peer, "peer")
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} does not run BGP")
            container = self._resolve_container(device)
            if view == "advertised_routes":
                safe_prefix = self._validate_prefix(prefix) if prefix else None
                result = self._docker_exec(
                    container,
                    ["vtysh", "-c", f"show bgp ipv4 unicast neighbors {safe_peer} advertised-routes json"],
                    timeout=30,
                )
                if result.returncode:
                    return ToolResult(success=False, data=None, error=result.stderr)
                advertised = json.loads(result.stdout)
                routes = advertised.get("advertisedRoutes", {})
                if safe_prefix:
                    routes = {key: value for key, value in routes.items() if key == safe_prefix}
                return ToolResult(
                    success=True,
                    data={
                        "device": device,
                        "peer": safe_peer,
                        "prefix": safe_prefix,
                        "view": view,
                        "advertised_routes": routes,
                        "collected_at": datetime.now(UTC).isoformat(),
                    },
                )
            detail_result = self._docker_exec(
                container,
                ["vtysh", "-c", f"show ip bgp neighbors {safe_peer}"],
                timeout=15,
            )
            if detail_result.returncode != 0:
                return ToolResult(success=False, data=None, error=detail_result.stderr)
            detail = self._parse_bgp_neighbor_detail(detail_result.stdout)
            summary_result = self._docker_exec(container, ["vtysh", "-c", "show ip bgp summary"], timeout=15)
            summary = None
            if summary_result.returncode == 0:
                summary = next(
                    (row for row in parse_bgp_summary(summary_result.stdout) if row.get("neighbor") == safe_peer),
                    None,
                )
            if not detail and summary is None:
                return ToolResult(success=False, data=None, error=f"BGP neighbor {safe_peer} not found on {device}")
            summary = summary or {}
            return ToolResult(
                success=True,
                data={
                    "device": device,
                    "peer": safe_peer,
                    "state": summary.get("state") or detail.get("bgp_state"),
                    "peer_as": summary.get("asn") or detail.get("remote_as"),
                    "configured_as": detail.get("remote_as"),
                    "local_address": detail.get("local_address"),
                    "foreign_address": detail.get("foreign_address"),
                    "uptime": summary.get("up_down") or detail.get("uptime"),
                    "last_reset": detail.get("last_reset"),
                    "last_error": detail.get("last_error"),
                    "notifications": detail.get("notifications", []),
                    "update_source": detail.get("update_source"),
                    "message_counters": {
                        "received": summary.get("msg_rcvd"),
                        "sent": summary.get("msg_sent"),
                    },
                    "prefixes_received": summary.get("prefixes_received"),
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as exc:
            return ToolResult(success=False, data=None, error=str(exc))

    def _get_bgp_neighbor_detail(self, container: str, neighbor_ip: str) -> dict | None:
        """Fetch per-neighbor detail for a non-Established peer."""
        try:
            result = self._docker_exec(
                container,
                ["vtysh", "-c", f"show ip bgp neighbors {neighbor_ip}"],
                timeout=15,
            )
            if result.returncode != 0 or not result.stdout:
                return None
            return self._parse_bgp_neighbor_detail(result.stdout)
        except Exception:
            logger.debug("failed to fetch BGP neighbor detail for %s on %s", neighbor_ip, container, exc_info=True)
            return None

    @staticmethod
    def _parse_bgp_neighbor_detail(text: str) -> dict:
        """Extract key diagnostic fields from 'show ip bgp neighbors <ip>' output."""
        import re

        detail: dict = {}
        for line in text.splitlines():
            line_s = line.strip()
            if line_s.startswith("BGP state ="):
                m = re.search(r"BGP state = (\S+)", line_s)
                if m:
                    detail["bgp_state"] = m.group(1).rstrip(",")
            elif "Last reset" in line_s:
                detail["last_reset"] = line_s
            elif "Notification" in line_s and ("sent" in line_s or "rcvd" in line_s):
                detail.setdefault("notifications", []).append(line_s)
            elif "remote as" in line_s.lower():
                m = re.search(r"remote AS (\d+)", line_s, re.IGNORECASE)
                if m:
                    detail["remote_as"] = int(m.group(1))
            elif "Local host:" in line_s:
                detail["local_address"] = line_s.split("Local host:", 1)[1].split(",", 1)[0].strip()
            elif "Foreign host:" in line_s:
                detail["foreign_address"] = line_s.split("Foreign host:", 1)[1].split(",", 1)[0].strip()
            elif "update-source" in line_s.lower():
                detail["update_source"] = line_s
            elif "password" in line_s.lower() and "configured" in line_s.lower():
                detail["password_configured"] = True
            elif "Last error" in line_s or "last error" in line_s:
                detail["last_error"] = line_s
        return detail

    def get_route_table(
        self,
        device: str,
        prefix: str | None = None,
        format: str = "structured",
        max_routes: int = 100,
        max_lines: int = 500,
        view: str = "frr",
    ) -> ToolResult:
        try:
            self._validate_device_name(device)
            safe_format = (format or "raw").lower()
            if safe_format not in {"raw", "structured", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} is not a router")
            container = self._resolve_container(device)
            if view not in {"frr", "kernel"}:
                raise ValueError("view must be frr or kernel")
            if view == "kernel":
                safe_prefix = self._validate_prefix(prefix) if prefix else None
                command = ["ip", "-j", "-4", "route", "show"]
                if safe_prefix:
                    command += ["exact", safe_prefix]
                result = self._docker_exec(container, command, timeout=30)
                if result.returncode:
                    return ToolResult(success=False, data=None, error=result.stderr)
                rows = json.loads(result.stdout)
                limit = max(1, min(int(max_routes), 1000))
                structured = {
                    "device": device,
                    "prefix": safe_prefix,
                    "view": view,
                    "routes": rows[:limit],
                    "total_routes": len(rows),
                    "truncated": len(rows) > limit,
                    "collected_at": datetime.now(UTC).isoformat(),
                }
                if safe_format == "raw":
                    structured = {"device": device, "prefix": safe_prefix, "view": view, "route_table": result.stdout}
                elif safe_format == "both":
                    structured = {"device": device, "raw": {"route_table": result.stdout}, "structured": structured}
                return ToolResult(success=True, data=structured)
            if prefix:
                safe_prefix = self._validate_prefix(prefix)
                sr_command = f"show ip route {safe_prefix}"
            else:
                safe_prefix = None
                sr_command = "show ip route"
            result = self._docker_exec(container, ["vtysh", "-c", sr_command], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            route_text, route_meta = truncate_text_lines(result.stdout, max_lines)
            raw_data = {"device": device, "prefix": safe_prefix, "route_table": route_text, **route_meta}
            if safe_format in {"structured", "both"}:
                routes = parse_route_table(result.stdout)
                safe_max_routes = max(1, int(max_routes))
                structured = {
                    "device": device,
                    "prefix": safe_prefix,
                    "routes": routes[:safe_max_routes],
                    "route_count": len(routes),
                    "returned_routes": min(len(routes), safe_max_routes),
                    "truncated": len(routes) > safe_max_routes,
                }
                if not routes:
                    structured["warning"] = "Unable to parse route table; check raw output."
                if safe_format == "structured":
                    return ToolResult(success=True, data=structured)
                return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
            return ToolResult(success=True, data=raw_data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_device_config(self, device: str, section: str = "", max_lines: int = 500) -> ToolResult:
        """Retrieve running configuration from a SONiC/FRR device via vtysh."""
        try:
            self._validate_device_name(device)
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} does not support running-config")
            container = self._resolve_container(device)
            result = self._docker_exec(container, ["vtysh", "-c", "show running-config"], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            text = result.stdout
            if section:
                blocks = re.split(r"(?m)^!\s*$", text)
                text = "\n!\n".join(block.strip("\n") for block in blocks if section.lower() in block.lower())
            config_text, config_meta = truncate_text_lines(text, max_lines)
            return ToolResult(
                success=True,
                data={
                    "device": device,
                    "section": section or None,
                    "config": config_text,
                    **config_meta,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_bgp_rib(self, device: str, prefix: str | None = None, max_lines: int = 500) -> ToolResult:
        """Retrieve BGP RIB entries with AS path, origin, next-hop, local-pref details."""
        try:
            self._validate_device_name(device)
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} does not run BGP")
            container = self._resolve_container(device)
            if prefix:
                safe_prefix = self._validate_prefix(prefix)
                cmd = f"show ip bgp {safe_prefix}"
            else:
                cmd = "show ip bgp"
            result = self._docker_exec(container, ["vtysh", "-c", cmd], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            rib_text, rib_meta = truncate_text_lines(result.stdout, max_lines)
            return ToolResult(
                success=True,
                data={
                    "device": device,
                    "prefix": prefix,
                    "bgp_rib": rib_text,
                    **rib_meta,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def get_device_acl(self, device: str, view: str = "summary", max_lines: int = 300) -> ToolResult:
        """Retrieve ACL configuration from a SONiC device.

        Returns SONiC CONFIG_DB ACL tables/rules (visible via ``show acl
        table`` / ``show acl rule``) and iptables FORWARD rules so that the
        diagnosing agent can see any ACL-related misconfigurations.
        """
        try:
            self._validate_device_name(device)
            safe_view = (view or "summary").lower()
            if safe_view not in {"summary", "frr", "iptables", "all"}:
                return ToolResult(success=False, data=None, error=f"Invalid view: {view}")
            if not is_network_device_name(device):
                return ToolResult(success=False, data=None, error=f"Device {device} does not support ACLs")
            container = self._resolve_container(device)

            # SONiC ACL tables from CONFIG_DB (standard diagnostic path)
            sonic_acl_output = ""
            acl_table_result = self._docker_exec(
                container,
                ["show", "acl", "table"],
                timeout=30,
            )
            if acl_table_result.returncode == 0:
                sonic_acl_output += acl_table_result.stdout or ""
            acl_rule_result = self._docker_exec(
                container,
                ["show", "acl", "rule"],
                timeout=30,
            )
            if acl_rule_result.returncode == 0:
                if sonic_acl_output:
                    sonic_acl_output += "\n"
                sonic_acl_output += acl_rule_result.stdout or ""

            # iptables FORWARD chain (data-plane ACL rules)
            ipt_result = self._docker_exec(
                container,
                ["iptables", "-L", "FORWARD", "-n", "-v", "--line-numbers"],
                timeout=30,
            )
            iptables_rules = ipt_result.stdout if ipt_result.returncode == 0 else ""

            sonic_acl_output, sonic_meta = truncate_text_lines(sonic_acl_output, max_lines)
            iptables_rules, iptables_meta = truncate_text_lines(iptables_rules, max_lines)
            data = {
                "device": device,
                "view": safe_view,
                "sonic_acl_line_count": sonic_meta["total_lines"],
                "iptables_line_count": iptables_meta["total_lines"],
                "truncated": sonic_meta["truncated"] or iptables_meta["truncated"],
                "collected_at": datetime.now(UTC).isoformat(),
                "collection_errors": [
                    {"source": source, "error": result.stderr or "Command failed"}
                    for source, result in (
                        ("acl_tables", acl_table_result),
                        ("acl_rules", acl_rule_result),
                        ("iptables", ipt_result),
                    )
                    if result.returncode
                ],
            }
            if safe_view in {"summary", "frr", "all"}:
                data["sonic_acl_config"] = sonic_acl_output
                data["sonic_acl_truncated"] = sonic_meta["truncated"]
            if safe_view in {"summary", "iptables", "all"}:
                data["iptables_forward_rules"] = iptables_rules
                data["iptables_truncated"] = iptables_meta["truncated"]
            available = len(data["collection_errors"]) < 3
            return ToolResult(success=available, data=data, error=None if available else "ACL state unavailable")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
