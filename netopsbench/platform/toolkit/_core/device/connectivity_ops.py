"""Connectivity test device toolkit operations."""

from __future__ import annotations

import subprocess

from ..common import ToolResult


class ConnectivityOpsMixin:
    def traceroute(self, src: str, dst_ip: str) -> ToolResult:
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_dst_ip = self._validate_ip_address(dst_ip, field_name="destination IP")
            container = self._resolve_container(safe_src, field_name="source")
            result = self._docker_exec(container, ["traceroute", "-n", "-w", "2", safe_dst_ip], timeout=60)
            return ToolResult(
                success=True,
                data={
                    "source": safe_src,
                    "destination": safe_dst_ip,
                    "traceroute": result.stdout if result.stdout else result.stderr,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Traceroute timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def ping_test(
        self, src: str, dst_ip: str, count: int = 5, payload_size: int | None = None, dont_fragment: bool = False
    ) -> ToolResult:
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_dst_ip = self._validate_ip_address(dst_ip, field_name="destination IP")
            safe_count = max(1, min(int(count), 20))
            safe_payload_size = None if payload_size is None else max(0, min(int(payload_size), 65507))
            container = self._resolve_container(safe_src, field_name="source")
            cmd = ["ping", "-c", str(safe_count), "-W", "2"]
            if safe_payload_size is not None:
                cmd.extend(["-s", str(safe_payload_size)])
            if bool(dont_fragment):
                cmd.extend(["-M", "do"])
            cmd.append(safe_dst_ip)
            result = self._docker_exec(container, cmd, timeout=30)
            return ToolResult(
                success=True,
                data={
                    "source": safe_src,
                    "destination": safe_dst_ip,
                    "count": safe_count,
                    "payload_size": safe_payload_size,
                    "dont_fragment": bool(dont_fragment),
                    "output": result.stdout if result.stdout else result.stderr,
                    "return_code": result.returncode,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Ping timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
