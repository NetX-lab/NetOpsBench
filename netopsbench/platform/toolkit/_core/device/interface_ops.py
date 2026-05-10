"""Interface-oriented device toolkit operations."""

from __future__ import annotations

import subprocess

from ..common import ToolResult


class InterfaceOpsMixin:
    def get_device_interfaces(self, device: str, format: str = "structured") -> ToolResult:
        try:
            self._validate_device_name(device)
            safe_format = (format or "raw").lower()
            if safe_format not in {"raw", "structured", "summary", "both"}:
                return ToolResult(success=False, data=None, error=f"Invalid format: {format}")
            container = self._resolve_container(device)
            if device.startswith(("spine", "leaf")):
                brief_result = self._docker_exec(container, ["bash", "-lc", "show interfaces status"], timeout=30)
                detail_result = self._docker_exec(container, ["bash", "-lc", "show interfaces counters"], timeout=30)
                if brief_result.returncode != 0 and detail_result.returncode != 0:
                    fallback = self._docker_exec(container, ["ip", "-s", "link", "show"], timeout=30)
                    if fallback.returncode != 0:
                        return ToolResult(success=False, data=None, error=fallback.stderr)
                    raw_data = {
                        "device": device,
                        "brief": "show interfaces status unavailable; using ip link",
                        "detail": fallback.stdout,
                    }
                    structured = self._parse_ip_link_stats(fallback.stdout)
                    if safe_format == "summary":
                        return ToolResult(success=True, data=self._summarize_interface_data(device, structured))
                    if safe_format == "structured":
                        return ToolResult(success=True, data={"device": device, "interfaces": structured})
                    if safe_format == "both":
                        return ToolResult(
                            success=True, data={"device": device, "raw": raw_data, "structured": structured}
                        )
                    return ToolResult(success=True, data=raw_data)
                raw_data = {
                    "device": device,
                    "brief": brief_result.stdout if brief_result.returncode == 0 else "Unable to get brief",
                    "detail": detail_result.stdout if detail_result.returncode == 0 else "Unable to get details",
                }
                if safe_format in {"structured", "summary", "both"}:
                    status_rows = self._parse_table(brief_result.stdout if brief_result.returncode == 0 else "")
                    counter_rows = self._parse_table(detail_result.stdout if detail_result.returncode == 0 else "")
                    merged = self._merge_interface_tables(status_rows, counter_rows)
                    structured = {"device": device, "interfaces": merged}
                    if not merged:
                        structured["warning"] = "Unable to parse interface tables; check raw output."
                    if safe_format == "summary":
                        return ToolResult(success=True, data=self._summarize_interface_data(device, merged))
                    if safe_format == "structured":
                        return ToolResult(success=True, data=structured)
                    return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
                return ToolResult(success=True, data=raw_data)
            result = self._docker_exec(container, ["ip", "-s", "link", "show"], timeout=30)
            if result.returncode != 0:
                return ToolResult(success=False, data=None, error=result.stderr)
            raw_data = {"device": device, "interfaces": result.stdout}
            if safe_format in {"structured", "summary", "both"}:
                structured = self._parse_ip_link_stats(result.stdout)
                if safe_format == "summary":
                    return ToolResult(success=True, data=self._summarize_interface_data(device, structured))
                if safe_format == "structured":
                    return ToolResult(success=True, data={"device": device, "interfaces": structured})
                return ToolResult(success=True, data={"device": device, "raw": raw_data, "structured": structured})
            return ToolResult(success=True, data=raw_data)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Command timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

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
