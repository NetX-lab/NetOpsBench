"""Routing-oriented device toolkit operations."""

from __future__ import annotations

import subprocess

from netopsbench.logging_utils import get_logger

from ..common import ToolResult, truncate_text_lines

logger = get_logger(__name__)


class RoutingOpsMixin:
    def get_bgp_neighbors(self, device: str, format: str = "structured") -> ToolResult:
        try:
            self._validate_device_name(device)
            safe_format = (format or "raw").lower()
            if safe_format not in {"raw", "structured", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            if not device.startswith(("spine", "leaf")):
                return ToolResult(success=False, data=None, error=f"Device {device} does not run BGP")
            container = self._resolve_container(device)
            result = self._docker_exec(container, ["vtysh", "-c", "show ip bgp summary"], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            raw_data = {"device": device, "bgp_neighbors": result.stdout}
            if safe_format in {"structured", "both"}:
                neighbors = self._parse_bgp_summary(result.stdout)
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
            elif "remote AS" in line_s.lower():
                m = re.search(r"remote AS (\d+)", line_s)
                if m:
                    detail["remote_as"] = int(m.group(1))
            elif "Local host:" in line_s:
                detail["local_host"] = line_s
            elif "Foreign host:" in line_s:
                detail["foreign_host"] = line_s
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
    ) -> ToolResult:
        try:
            self._validate_device_name(device)
            safe_format = (format or "raw").lower()
            if safe_format not in {"raw", "structured", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            if not device.startswith(("spine", "leaf")):
                return ToolResult(success=False, data=None, error=f"Device {device} is not a router")
            container = self._resolve_container(device)
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
                routes = self._parse_route_table(result.stdout)
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

    def get_all_bgp_status(self) -> ToolResult:
        results = {}
        devices = []
        if self.topology_metadata:
            for spine in self.topology_metadata.get("devices", {}).get("spines", []):
                devices.append(spine["name"])
            for leaf in self.topology_metadata.get("devices", {}).get("leafs", []):
                devices.append(leaf["name"])
        else:
            devices = [name for name in self.container_names.keys() if name.startswith(("spine", "leaf"))]
        for device in devices:
            result = self.get_bgp_neighbors(device)
            results[device] = result.data if result.success else {"error": result.error}
        return ToolResult(success=True, data=results)

    def get_device_config(self, device: str, section: str = "", max_lines: int = 500) -> ToolResult:
        """Retrieve running configuration from a SONiC/FRR device via vtysh."""
        try:
            self._validate_device_name(device)
            if not device.startswith(("spine", "leaf")):
                return ToolResult(success=False, data=None, error=f"Device {device} does not support running-config")
            container = self._resolve_container(device)
            if section:
                import re

                safe_section = re.sub(r"[^a-zA-Z0-9 _-]", "", section)
                cmd = f"show running-config | section {safe_section}"
            else:
                cmd = "show running-config"
            result = self._docker_exec(container, ["vtysh", "-c", cmd], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            config_text, config_meta = truncate_text_lines(result.stdout, max_lines)
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
            if not device.startswith(("spine", "leaf")):
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
            if not device.startswith(("spine", "leaf")):
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
            }
            if safe_view in {"summary", "frr", "all"}:
                data["sonic_acl_config"] = sonic_acl_output
                data["sonic_acl_truncated"] = sonic_meta["truncated"]
            if safe_view in {"summary", "iptables", "all"}:
                data["iptables_forward_rules"] = iptables_rules
                data["iptables_truncated"] = iptables_meta["truncated"]
            return ToolResult(success=True, data=data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
