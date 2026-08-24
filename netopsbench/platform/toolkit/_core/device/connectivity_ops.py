"""Connectivity test device toolkit operations."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from statistics import median

from netopsbench.models.topology import DeviceRole
from netopsbench.platform.utils.interface_names import (
    are_interfaces_equivalent,
    to_linux_interface,
    to_sonic_interface,
)

from ..common import ToolResult


class ConnectivityOpsMixin:
    _BAD_ICMP_CHECKSUM_RE = re.compile(
        r"wrong icmp cksum|bad icmp cksum|bad icmp checksum",
        re.IGNORECASE,
    )
    _TCPDUMP_TIMESTAMP_RE = re.compile(r"(?m)^(?P<timestamp>\d+(?:\.\d+)?)\s+")
    _ICMP_REQUEST_ID_SEQUENCE_RE = re.compile(
        r"ICMP echo request,.*?\bid\s+(?P<identifier>\d+)\b"
        r".*?\b(?:seq\s+|icmp_seq[=\s]+)(?P<sequence>\d+)\b",
        re.IGNORECASE | re.DOTALL,
    )

    def traceroute(self, src: str, dst_ip: str) -> ToolResult:
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_dst_ip = self._validate_ip_address(dst_ip, field_name="destination IP")
            source_device = self.manifest.device(safe_src)
            if source_device is None:
                raise ValueError(f"Unknown source: {safe_src}")
            if source_device.role is not DeviceRole.CLIENT:
                raise ValueError(
                    f"Traceroute source must be a client device, got {safe_src} ({source_device.role.value})"
                )
            container = self._resolve_container(safe_src, field_name="source")
            result = self._docker_exec(
                container,
                ["traceroute", "-n", "-q", "1", "-w", "1", "-m", "8", safe_dst_ip],
                timeout=12,
            )
            if result.returncode != 0:
                output = (result.stderr or result.stdout or "unknown error").strip()
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Traceroute failed on {safe_src} (exit {result.returncode}): {output}",
                )
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
        self,
        src: str,
        dst_ip: str,
        count: int = 5,
        payload_size: int | None = None,
        dont_fragment: bool = False,
        source_interface: str | None = None,
    ) -> ToolResult:
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_dst_ip = self._validate_ip_address(dst_ip, field_name="destination IP")
            safe_count = max(1, min(int(count), 20))
            safe_payload_size = None if payload_size is None else max(0, min(int(payload_size), 65507))
            safe_source_interface = self._resolve_source_interface(safe_src, source_interface)
            container = self._resolve_container(safe_src, field_name="source")
            cmd = ["ping", "-c", str(safe_count), "-W", "2"]
            if safe_source_interface is not None:
                cmd.extend(["-I", safe_source_interface])
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
                    "source_interface": safe_source_interface,
                    "output": result.stdout if result.stdout else result.stderr,
                    "return_code": result.returncode,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Ping timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def _resolve_source_interface(self, source: str, interface: str | None) -> str | None:
        if interface is None:
            return None
        safe_interface = self._validate_interface_name(interface)
        source_device = self.manifest.device(source)
        if source_device is None:
            raise ValueError(f"Unknown source: {source}")
        endpoint = next(
            (
                endpoint
                for link in self.manifest.links
                for endpoint in link.endpoints
                if endpoint.device == source and are_interfaces_equivalent(endpoint.interface, safe_interface)
            ),
            None,
        )
        if endpoint is None:
            raise ValueError(f"Interface {safe_interface} does not belong to source {source}")
        # Network containers route through the addressed SONiC front-panel
        # interface.  The parallel containerlab ethN veth is deliberately
        # unnumbered and cannot provide a returnable ICMP source address.
        if source_device.role is DeviceRole.CLIENT:
            return to_linux_interface(endpoint.interface)
        return to_sonic_interface(endpoint.interface)

    def ping_link_test(
        self,
        src: str,
        target_device: str,
        source_interface: str,
        target_interface: str,
        count: int = 5,
        payload_size: int | None = None,
        dont_fragment: bool = False,
    ) -> ToolResult:
        """Ping a live peer interface to isolate one physical topology link."""
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_target = self._validate_device_name(target_device, field_name="target device")
            safe_source_interface = self._validate_interface_name(source_interface)
            safe_target_interface = self._validate_interface_name(target_interface)
            source_device = self.manifest.device(safe_src)
            target = self.manifest.device(safe_target)
            if source_device is None or target is None:
                raise ValueError("Unknown source or target device")
            if source_device.role is DeviceRole.CLIENT or target.role is DeviceRole.CLIENT:
                raise ValueError("Link ping endpoints must both be network devices")

            matched_link = next(
                (
                    link
                    for link in self.manifest.links
                    if any(
                        endpoint.device == safe_src
                        and are_interfaces_equivalent(endpoint.interface, safe_source_interface)
                        for endpoint in link.endpoints
                    )
                    and any(
                        endpoint.device == safe_target
                        and are_interfaces_equivalent(endpoint.interface, safe_target_interface)
                        for endpoint in link.endpoints
                    )
                ),
                None,
            )
            if matched_link is None:
                raise ValueError(
                    f"Interfaces are not physical peers: {safe_src}:{safe_source_interface} and "
                    f"{safe_target}:{safe_target_interface}"
                )

            source_runtime_interface = to_sonic_interface(safe_source_interface)
            target_runtime_interface = to_sonic_interface(safe_target_interface)
            safe_count = max(1, min(int(count), 20))
            safe_payload_size = None if payload_size is None else max(0, min(int(payload_size), 65507))
            source_container = self._resolve_container(safe_src, field_name="source")
            target_container = self._resolve_container(safe_target, field_name="target device")
            address_result = self._docker_exec(
                target_container,
                ["ip", "-4", "-o", "address", "show", "dev", target_runtime_interface, "scope", "global"],
                timeout=10,
            )
            address_match = re.search(r"\binet\s+(\d+(?:\.\d+){3})/\d+", address_result.stdout or "")
            destination_ip = address_match.group(1) if address_result.returncode == 0 and address_match else None
            if destination_ip is None:
                # A down target cannot answer the introspection command. On a
                # validated point-to-point /30 or /31 link, derive the peer
                # address from the surviving source endpoint instead.
                source_address = self._docker_exec(
                    source_container,
                    ["ip", "-4", "-o", "address", "show", "dev", source_runtime_interface, "scope", "global"],
                    timeout=10,
                )
                source_match = re.search(r"\binet\s+(\d+(?:\.\d+){3}/\d+)", source_address.stdout or "")
                if source_address.returncode == 0 and source_match:
                    source_ip = ipaddress.ip_interface(source_match.group(1))
                    if source_ip.network.prefixlen in {30, 31}:
                        destination_ip = next(
                            (str(host) for host in source_ip.network.hosts() if host != source_ip.ip),
                            None,
                        )
                if destination_ip is None:
                    detail = (address_result.stderr or address_result.stdout or "no global IPv4 address").strip()
                    return ToolResult(
                        success=False,
                        data=None,
                        error=(
                            f"Unable to resolve peer interface address on "
                            f"{safe_target}:{target_runtime_interface}: {detail}"
                        ),
                    )
            command = [
                "ping",
                "-c",
                str(safe_count),
                "-W",
                "2",
                "-I",
                source_runtime_interface,
            ]
            if safe_payload_size is not None:
                command.extend(["-s", str(safe_payload_size)])
            if dont_fragment:
                command.extend(["-M", "do"])
            command.append(destination_ip)
            result = self._docker_exec(source_container, command, timeout=30)
            return ToolResult(
                success=True,
                data={
                    "source": safe_src,
                    "destination": destination_ip,
                    "target_device": safe_target,
                    "count": safe_count,
                    "payload_size": safe_payload_size,
                    "dont_fragment": bool(dont_fragment),
                    "source_interface": source_runtime_interface,
                    "target_interface": target_runtime_interface,
                    "output": result.stdout if result.stdout else result.stderr,
                    "return_code": result.returncode,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Link ping timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    @classmethod
    def _icmp_capture_timestamps(cls, output: str) -> dict[tuple[int, int], float]:
        """Return first timestamp by ICMP identifier/sequence from tcpdump.

        ``tcpdump -i any -vvv`` is deliberately used by the live latency
        probe because SONiC dataplane interface names vary between images.
        Depending on the link type and tcpdump version, that output is either
        one line per packet or a timestamp/interface header followed by an
        indented IP/ICMP line.  Parse timestamp-delimited packet blocks so the
        observation contract does not depend on either presentation format.
        """
        timestamps: dict[tuple[int, int], float] = {}
        headers = list(cls._TCPDUMP_TIMESTAMP_RE.finditer(output))
        for index, header in enumerate(headers):
            block_end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
            packet = output[header.end() : block_end]
            request = cls._ICMP_REQUEST_ID_SEQUENCE_RE.search(packet)
            if request is None:
                continue
            key = (int(request.group("identifier")), int(request.group("sequence")))
            # ``any`` may expose the same skb on more than one hook.  The
            # earliest timestamp is the correct boundary observation.
            timestamp = float(header.group("timestamp"))
            timestamps[key] = min(timestamp, timestamps.get(key, timestamp))
        return timestamps

    def _run_one_way_latency_direction(
        self,
        *,
        source_device: str,
        source_l3_interface: str,
        source_capture_interface: str,
        source_ip: str,
        target_device: str,
        target_l3_interface: str,
        target_capture_interface: str,
        target_ip: str,
        count: int,
    ) -> dict:
        """Measure bounded one-way link transit using shared host clock timestamps."""
        source_container = self._resolve_container(source_device, field_name="source")
        target_container = self._resolve_container(target_device, field_name="target")
        packet_filter = [
            "icmp",
            "and",
            "src",
            "host",
            source_ip,
            "and",
            "dst",
            "host",
            target_ip,
            "and",
            "icmp[icmptype]",
            "=",
            "icmp-echo",
        ]

        def capture(interface: str) -> list[str]:
            return [
                "timeout",
                "10",
                "tcpdump",
                "-tt",
                "-nn",
                "-l",
                "-i",
                interface,
                "-c",
                str(count),
                "-vvv",
                *packet_filter,
            ]

        ping_command = [
            "ping",
            "-c",
            str(count),
            "-W",
            "2",
            "-i",
            "0.05",
            "-I",
            source_l3_interface,
            target_ip,
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            source_future = executor.submit(
                self._docker_exec,
                source_container,
                capture(source_capture_interface),
                12,
            )
            target_future = executor.submit(
                self._docker_exec,
                target_container,
                capture(target_capture_interface),
                12,
            )
            time.sleep(0.2)
            sender = self._docker_exec(source_container, ping_command, timeout=30)
            try:
                source_capture = source_future.result(timeout=15)
                target_capture = target_future.result(timeout=15)
            except FutureTimeoutError as exc:
                source_future.cancel()
                target_future.cancel()
                raise subprocess.TimeoutExpired(capture(source_capture_interface), timeout=12) from exc

        if sender.returncode not in {0, 1}:
            detail = (sender.stderr or sender.stdout or "unknown sender error").strip()
            raise RuntimeError(f"One-way latency sender failed: {detail}")
        source_output = "\n".join(part for part in (source_capture.stdout, source_capture.stderr) if part)
        target_output = "\n".join(part for part in (target_capture.stdout, target_capture.stderr) if part)
        source_times = self._icmp_capture_timestamps(source_output)
        target_times = self._icmp_capture_timestamps(target_output)
        matched = sorted(source_times.keys() & target_times.keys())
        samples_ms = [
            (target_times[packet_id] - source_times[packet_id]) * 1000.0
            for packet_id in matched
            if target_times[packet_id] >= source_times[packet_id]
        ]
        if not samples_ms:
            raise RuntimeError(
                "One-way latency capture produced no packet matches "
                f"(source_parsed={len(source_times)}, target_parsed={len(target_times)}, "
                f"source_capture={source_capture.returncode}, target_capture={target_capture.returncode})"
            )
        ordered = sorted(samples_ms)
        p95 = ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]
        return {
            "source": source_device,
            "source_interface": source_l3_interface,
            "source_capture_interface": source_capture_interface,
            "target_device": target_device,
            "target_interface": target_l3_interface,
            "target_capture_interface": target_capture_interface,
            "sample_count": len(ordered),
            "source_packets_parsed": len(source_times),
            "target_packets_parsed": len(target_times),
            "matched_packets": len(ordered),
            "median_ms": float(median(ordered)),
            "p95_ms": float(p95),
            "min_ms": float(ordered[0]),
            "max_ms": float(ordered[-1]),
            "samples_ms": ordered,
        }

    def latency_link_test(
        self,
        device_a: str,
        interface_a: str,
        device_b: str,
        interface_b: str,
        count: int = 7,
    ) -> ToolResult:
        """Measure one-way transit in both directions on a validated physical link."""
        try:
            safe_a = self._validate_device_name(device_a, field_name="device A")
            safe_b = self._validate_device_name(device_b, field_name="device B")
            safe_if_a = self._validate_interface_name(interface_a)
            safe_if_b = self._validate_interface_name(interface_b)
            endpoint_a = self.manifest.device(safe_a)
            endpoint_b = self.manifest.device(safe_b)
            if endpoint_a is None or endpoint_b is None:
                raise ValueError("Unknown latency link endpoint")
            matched_link = next(
                (
                    link
                    for link in self.manifest.links
                    if any(
                        endpoint.device == safe_a and are_interfaces_equivalent(endpoint.interface, safe_if_a)
                        for endpoint in link.endpoints
                    )
                    and any(
                        endpoint.device == safe_b and are_interfaces_equivalent(endpoint.interface, safe_if_b)
                        for endpoint in link.endpoints
                    )
                ),
                None,
            )
            if matched_link is None:
                raise ValueError(f"Interfaces are not physical peers: {safe_a}:{safe_if_a} and {safe_b}:{safe_if_b}")
            l3_if_a = (
                to_linux_interface(safe_if_a) if endpoint_a.role is DeviceRole.CLIENT else to_sonic_interface(safe_if_a)
            )
            l3_if_b = (
                to_linux_interface(safe_if_b) if endpoint_b.role is DeviceRole.CLIENT else to_sonic_interface(safe_if_b)
            )
            # Source selection must use the addressed L3 interface, but packet
            # capture must not assume a specific SONiC/containerlab dataplane
            # interface name.  SONiC images can expose the same physical port
            # through EthernetX, ethN, a bridge, or a team device depending on
            # image/runtime version.  The address-qualified BPF filter makes
            # ``any`` unambiguous while preserving the exact physical-link
            # guarantee supplied by the validated peer IP and ``-I`` source.
            # Docker containers share the host wall clock, so timestamps from
            # the two namespaces remain directly comparable.
            capture_if_a = "any"
            capture_if_b = "any"

            def interface_ip(device: str, interface: str) -> str:
                container = self._resolve_container(device, field_name="link endpoint")
                result = self._docker_exec(
                    container,
                    ["ip", "-4", "-o", "address", "show", "dev", interface, "scope", "global"],
                    timeout=10,
                )
                match = re.search(r"\binet\s+(\d+(?:\.\d+){3})/\d+", result.stdout or "")
                if result.returncode != 0 or match is None:
                    detail = (result.stderr or result.stdout or "no global IPv4 address").strip()
                    raise RuntimeError(f"Unable to resolve {device}:{interface}: {detail}")
                return match.group(1)

            ip_a = interface_ip(safe_a, l3_if_a)
            ip_b = interface_ip(safe_b, l3_if_b)
            safe_count = max(3, min(int(count), 20))
            directions = [
                self._run_one_way_latency_direction(
                    source_device=safe_a,
                    source_l3_interface=l3_if_a,
                    source_capture_interface=capture_if_a,
                    source_ip=ip_a,
                    target_device=safe_b,
                    target_l3_interface=l3_if_b,
                    target_capture_interface=capture_if_b,
                    target_ip=ip_b,
                    count=safe_count,
                ),
                self._run_one_way_latency_direction(
                    source_device=safe_b,
                    source_l3_interface=l3_if_b,
                    source_capture_interface=capture_if_b,
                    source_ip=ip_b,
                    target_device=safe_a,
                    target_l3_interface=l3_if_a,
                    target_capture_interface=capture_if_a,
                    target_ip=ip_a,
                    count=safe_count,
                ),
            ]
            return ToolResult(
                success=True,
                data={
                    "supported": True,
                    "device_a": safe_a,
                    "interface_a": to_sonic_interface(safe_if_a),
                    "device_b": safe_b,
                    "interface_b": to_sonic_interface(safe_if_b),
                    "count_per_direction": safe_count,
                    "directions": directions,
                    "method": "bounded_bidirectional_one_way_ingress_capture",
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Link one-way latency capture timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def _run_integrity_direction(
        self,
        *,
        source_device: str,
        source_l3_interface: str,
        source_capture_interface: str,
        source_ip: str,
        target_device: str,
        target_l3_interface: str,
        target_capture_interface: str,
        target_ip: str,
        count: int,
    ) -> dict:
        """Send bounded ICMP payloads while checking checksum at ingress."""
        source_container = self._resolve_container(source_device, field_name="source")
        target_container = self._resolve_container(target_device, field_name="target")
        capture_command = [
            "timeout",
            "10",
            "tcpdump",
            "-tttt",
            "-n",
            "-i",
            target_capture_interface,
            "-c",
            str(count),
            "-vvv",
            "icmp",
            "and",
            "src",
            "host",
            source_ip,
            "and",
            "dst",
            "host",
            target_ip,
            "and",
            "icmp[icmptype]",
            "=",
            "icmp-echo",
        ]
        ping_command = [
            "ping",
            "-c",
            str(count),
            "-W",
            "2",
            "-i",
            "0.05",
            "-I",
            source_l3_interface,
            "-s",
            "256",
            target_ip,
        ]
        with ThreadPoolExecutor(max_workers=1) as executor:
            capture_future = executor.submit(self._docker_exec, target_container, capture_command, 12)
            # Give tcpdump a bounded head start so the first sequence is not
            # missed. No process or network configuration is changed.
            time.sleep(0.2)
            sender = self._docker_exec(source_container, ping_command, timeout=30)
            try:
                capture = capture_future.result(timeout=15)
            except FutureTimeoutError as exc:
                capture_future.cancel()
                raise subprocess.TimeoutExpired(capture_command, timeout=12) from exc

        output = "\n".join(part for part in (capture.stdout, capture.stderr) if part)
        captured_match = re.search(r"(?m)^(\d+) packets captured\s*$", output)
        captured = int(captured_match.group(1)) if captured_match else 0
        bad_checksums = len(self._BAD_ICMP_CHECKSUM_RE.findall(output))
        sender_summary = re.search(r"(?m)^(\d+) packets transmitted", sender.stdout or sender.stderr or "")
        packets_sent = int(sender_summary.group(1)) if sender_summary else count
        if capture.returncode not in {0, 124} and captured == 0:
            detail = (capture.stderr or capture.stdout or "unknown capture error").strip()
            raise RuntimeError(f"Payload-integrity capture failed: {detail}")
        if sender.returncode not in {0, 1}:
            detail = (sender.stderr or sender.stdout or "unknown sender error").strip()
            raise RuntimeError(f"Payload-integrity sender failed: {detail}")
        return {
            "source": source_device,
            "source_interface": source_l3_interface,
            "source_capture_interface": source_capture_interface,
            "source_ip": source_ip,
            "destination": target_ip,
            "target_device": target_device,
            "target_interface": target_l3_interface,
            "target_capture_interface": target_capture_interface,
            "packets_sent": packets_sent,
            "packets_observed": captured,
            "missing_packets": max(0, packets_sent - captured),
            "integrity_complete": packets_sent > 0 and captured >= packets_sent,
            "checksum_valid": False if bad_checksums else True if captured else None,
            "checksum_failures": bad_checksums,
        }

    def payload_integrity_test(self, src: str, dst_ip: str, count: int = 5) -> ToolResult:
        """Actively send bounded payloads and verify ICMP checksum at ingress.

        Linux ping supplies a deterministic sequence-bearing payload protected
        by the ICMP checksum. Capturing at destination ingress observes damaged
        payloads before the host stack discards them, separating corruption
        from packets that never arrive without changing network state.
        """
        try:
            safe_src = self._validate_device_name(src, field_name="source")
            safe_dst_ip = self._validate_ip_address(dst_ip, field_name="destination IP")
            safe_count = max(1, min(int(count), 20))
            source = self.manifest.device(safe_src)
            if source is None or source.role is not DeviceRole.CLIENT or not source.data_ip:
                raise ValueError(f"Payload-integrity source must be a client device, got {safe_src}")
            destination = next(
                (client for client in self.manifest.clients() if str(client.data_ip) == safe_dst_ip),
                None,
            )
            if destination is None:
                raise ValueError(f"Payload-integrity destination must be a client data IP, got {safe_dst_ip}")
            observation = self._run_integrity_direction(
                source_device=safe_src,
                source_l3_interface="eth1",
                source_capture_interface="eth1",
                source_ip=str(source.data_ip),
                target_device=destination.name,
                target_l3_interface="eth1",
                target_capture_interface="eth1",
                target_ip=safe_dst_ip,
                count=safe_count,
            )
            return ToolResult(
                success=True,
                data={
                    "source": safe_src,
                    "destination": safe_dst_ip,
                    "supported": True,
                    "observation_complete": True,
                    "received": observation["packets_observed"] > 0,
                    "sequence": None,
                    "checksum_valid": observation["checksum_valid"],
                    "receive_timestamp": None,
                    "packets_sent": observation["packets_sent"],
                    "packets_observed": observation["packets_observed"],
                    "missing_packets": observation["missing_packets"],
                    "integrity_complete": observation["integrity_complete"],
                    "checksum_failures": observation["checksum_failures"],
                    "method": "bounded_active_destination_ingress_icmp_checksum_capture",
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Payload-integrity capture timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))

    def payload_integrity_link_test(
        self,
        device_a: str,
        interface_a: str,
        device_b: str,
        interface_b: str,
        count: int = 20,
    ) -> ToolResult:
        """Verify payload integrity in both directions on one physical link."""
        try:
            safe_a = self._validate_device_name(device_a, field_name="device A")
            safe_b = self._validate_device_name(device_b, field_name="device B")
            safe_if_a = self._validate_interface_name(interface_a)
            safe_if_b = self._validate_interface_name(interface_b)
            endpoint_a = self.manifest.device(safe_a)
            endpoint_b = self.manifest.device(safe_b)
            if endpoint_a is None or endpoint_b is None:
                raise ValueError("Unknown payload-integrity link endpoint")
            matched_link = next(
                (
                    link
                    for link in self.manifest.links
                    if any(
                        endpoint.device == safe_a and are_interfaces_equivalent(endpoint.interface, safe_if_a)
                        for endpoint in link.endpoints
                    )
                    and any(
                        endpoint.device == safe_b and are_interfaces_equivalent(endpoint.interface, safe_if_b)
                        for endpoint in link.endpoints
                    )
                ),
                None,
            )
            if matched_link is None:
                raise ValueError(f"Interfaces are not physical peers: {safe_a}:{safe_if_a} and {safe_b}:{safe_if_b}")
            l3_if_a = (
                to_linux_interface(safe_if_a) if endpoint_a.role is DeviceRole.CLIENT else to_sonic_interface(safe_if_a)
            )
            l3_if_b = (
                to_linux_interface(safe_if_b) if endpoint_b.role is DeviceRole.CLIENT else to_sonic_interface(safe_if_b)
            )
            capture_if_a = to_linux_interface(safe_if_a)
            capture_if_b = to_linux_interface(safe_if_b)

            def interface_ip(device: str, interface: str) -> str:
                container = self._resolve_container(device, field_name="link endpoint")
                result = self._docker_exec(
                    container,
                    ["ip", "-4", "-o", "address", "show", "dev", interface, "scope", "global"],
                    timeout=10,
                )
                match = re.search(r"\binet\s+(\d+(?:\.\d+){3})/\d+", result.stdout or "")
                if result.returncode != 0 or match is None:
                    detail = (result.stderr or result.stdout or "no global IPv4 address").strip()
                    raise RuntimeError(f"Unable to resolve {device}:{interface}: {detail}")
                return match.group(1)

            ip_a = interface_ip(safe_a, l3_if_a)
            ip_b = interface_ip(safe_b, l3_if_b)
            # Corruption is commonly a low-rate stochastic impairment.  A
            # 20-packet sample has a 12% miss probability at 10% corruption;
            # allow a still-bounded 60 packets per direction so callers can
            # obtain useful negative evidence without changing network state.
            safe_count = max(1, min(int(count), 60))
            directions = [
                self._run_integrity_direction(
                    source_device=safe_a,
                    source_l3_interface=l3_if_a,
                    source_capture_interface=capture_if_a,
                    source_ip=ip_a,
                    target_device=safe_b,
                    target_l3_interface=l3_if_b,
                    target_capture_interface=capture_if_b,
                    target_ip=ip_b,
                    count=safe_count,
                ),
                self._run_integrity_direction(
                    source_device=safe_b,
                    source_l3_interface=l3_if_b,
                    source_capture_interface=capture_if_b,
                    source_ip=ip_b,
                    target_device=safe_a,
                    target_l3_interface=l3_if_a,
                    target_capture_interface=capture_if_a,
                    target_ip=ip_a,
                    count=safe_count,
                ),
            ]
            return ToolResult(
                success=True,
                data={
                    "supported": True,
                    "device_a": safe_a,
                    "interface_a": to_sonic_interface(safe_if_a),
                    "device_b": safe_b,
                    "interface_b": to_sonic_interface(safe_if_b),
                    "count_per_direction": safe_count,
                    "directions": directions,
                    "checksum_failures": sum(item["checksum_failures"] for item in directions),
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, data=None, error="Link payload-integrity capture timed out")
        except Exception as e:
            return ToolResult(success=False, data=None, error=str(e))
