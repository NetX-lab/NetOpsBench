"""Pure iperf3 command construction for traffic execution."""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Protocol


class TrafficFlowLike(Protocol):
    flow_id: str
    dst_ip: str
    dst_port: int
    parallel: int
    protocol: str
    bandwidth: str
    udp_payload_len: int
    tcp_mss: int
    duration: int


class IperfCommandBuilder:
    """Build shell-safe server, client, and cleanup commands."""

    _RUNTIME_DIR = "/tmp/netopsbench-traffic"

    @staticmethod
    def _flow_running_function() -> list[str]:
        return [
            "flow_running() {",
            "  pid_file=$1",
            '  [ -r "$pid_file" ] || return 1',
            '  pid="$(cat "$pid_file" 2>/dev/null || true)"',
            '  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1',
            '  [ "$(cat "/proc/$pid/comm" 2>/dev/null || true)" = "iperf3" ] || return 1',
            '  tr "\\000" " " < "/proc/$pid/cmdline" 2>/dev/null | grep -q "^iperf3 -c "',
            "}",
        ]

    def client_args(self, flow: TrafficFlowLike) -> list[str]:
        args = [
            "iperf3",
            "-c",
            flow.dst_ip,
            "-p",
            str(flow.dst_port),
            "-P",
            str(flow.parallel),
        ]
        if flow.protocol == "udp":
            args.extend(["-u", "-b", flow.bandwidth, "-l", str(flow.udp_payload_len)])
        elif flow.bandwidth:
            args.extend(["-b", flow.bandwidth])
            if flow.tcp_mss > 0:
                args.extend(["-M", str(flow.tcp_mss)])
        args.extend(["-t", str(flow.duration)])
        return args

    def client_command(self, flow: TrafficFlowLike) -> str:
        return " ".join(shlex.quote(part) for part in self.client_args(flow))

    def source_batch_script(self, flows: Iterable[TrafficFlowLike]) -> str:
        flow_list = list(flows)
        commands = ["set -e", f"runtime_dir={shlex.quote(self._RUNTIME_DIR)}", 'mkdir -p "$runtime_dir"']
        commands.extend(self._flow_running_function())
        checks = []
        pid_files = []
        for flow in flow_list:
            command = self.client_command(flow)
            pid_file = f"{self._RUNTIME_DIR}/{flow.flow_id}.pid"
            quoted_pid_file = shlex.quote(pid_file)
            pid_files.append(quoted_pid_file)
            commands.extend(
                [
                    f"if ! flow_running {quoted_pid_file}; then",
                    f"  rm -f {quoted_pid_file}",
                    f"  nohup {command} >/dev/null 2>&1 </dev/null &",
                    f"  echo $! > {quoted_pid_file}",
                    "fi",
                ]
            )
            checks.append(f"  flow_running {quoted_pid_file} || missing=1")
        commands.extend(["for _attempt in 1 2 3 4 5; do", "  missing=0"])
        commands.extend(checks)
        commands.extend(
            [
                '  [ "$missing" -eq 0 ] && exit 0',
                "  sleep 0.1",
                "done",
                f"for pid_file in {' '.join(pid_files)}; do",
                '  flow_running "$pid_file" || rm -f "$pid_file"',
                "done",
                "exit 1",
            ]
        )
        return "\n".join(commands)

    def server_batch_script(self, ports: Iterable[int]) -> str:
        required_ports = " ".join(str(port) for port in sorted(set(ports)))
        return "\n".join(
            [
                "set -e",
                f"required_ports={shlex.quote(required_ports)}",
                'listeners="$(ss -lntH 2>/dev/null || true)"',
                "for port in $required_ports; do",
                '  if ! printf \'%s\\n\' "$listeners" | grep -q ":${port} "; then',
                '    iperf3 -s -D -p "$port"',
                "  fi",
                "done",
                "for _attempt in 1 2 3 4 5; do",
                '  listeners="$(ss -lntH 2>/dev/null || true)"',
                "  missing=0",
                "  for port in $required_ports; do",
                '    printf \'%s\\n\' "$listeners" | grep -q ":${port} " || missing=1',
                "  done",
                '  [ "$missing" -eq 0 ] && exit 0',
                "  sleep 0.1",
                "done",
                "exit 1",
            ]
        )

    def verify_runtime_script(
        self,
        flows: Iterable[TrafficFlowLike],
        server_ports: Iterable[int],
    ) -> str:
        """Verify the expected clients and listeners in one container."""
        flow_list = list(flows)
        required_ports = " ".join(str(port) for port in sorted(set(server_ports)))
        lines = ["set -e"]
        lines.extend(self._flow_running_function())
        for flow in flow_list:
            pid_file = shlex.quote(f"{self._RUNTIME_DIR}/{flow.flow_id}.pid")
            lines.append(f"flow_running {pid_file}")
        lines.extend(
            [
                f"required_ports={shlex.quote(required_ports)}",
                'listeners="$(ss -lntH 2>/dev/null || true)"',
                "for port in $required_ports; do",
                '  printf \'%s\\n\' "$listeners" | grep -q ":${port} "',
                "done",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def stop_all_script() -> str:
        """Stop NetOpsBench clients and servers on the reserved traffic ports."""
        runtime_dir = shlex.quote(IperfCommandBuilder._RUNTIME_DIR)
        lines = [f"runtime_dir={runtime_dir}", "stopped_pids="]
        lines.extend(IperfCommandBuilder._flow_running_function())
        lines.extend(
            [
                'for pid_file in "$runtime_dir"/*.pid; do',
                '  [ -e "$pid_file" ] || continue',
                '  if flow_running "$pid_file"; then',
                '    pid="$(cat "$pid_file")"',
                '    kill "$pid" 2>/dev/null || true',
                '    stopped_pids="$stopped_pids $pid"',
                "  fi",
                '  rm -f "$pid_file"',
                "done",
                "for process_dir in /proc/[0-9]*; do",
                '  pid="${process_dir##*/}"',
                '  [ "$(cat "$process_dir/comm" 2>/dev/null || true)" = "iperf3" ] || continue',
                "  cmdline=\"$(tr '\\000' ' ' < \"$process_dir/cmdline\" 2>/dev/null || true)\"",
                '  case "$cmdline" in',
                (
                    '    "iperf3 -c "*|"iperf3 -s -D "|"iperf3 -s "|'
                    '"iperf3 -s -D -p 5201 "*|'
                    '"iperf3 -s -D -p 5202 "*|"iperf3 -s -D -p 5203 "*|'
                    '"iperf3 -s -D -p 5204 "*|"iperf3 -s -p 5201 "*|'
                    '"iperf3 -s -p 5202 "*|"iperf3 -s -p 5203 "*|'
                    '"iperf3 -s -p 5204 "*)'
                ),
                '      kill "$pid" 2>/dev/null || true',
                '      stopped_pids="$stopped_pids $pid"',
                "      ;;",
                "  esac",
                "done",
                "for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do",
                "  running=0",
                "  for pid in $stopped_pids; do",
                '    kill -0 "$pid" 2>/dev/null && running=1',
                "  done",
                '  [ "$running" -eq 0 ] && break',
                "  sleep 0.1",
                "done",
                "for pid in $stopped_pids; do",
                '  if kill -0 "$pid" 2>/dev/null '
                '&& [ "$(cat "/proc/$pid/comm" 2>/dev/null || true)" = "iperf3" ]; then',
                '    kill -9 "$pid" 2>/dev/null || true',
                "  fi",
                "done",
                'rmdir "$runtime_dir" 2>/dev/null || true',
            ]
        )
        return "\n".join(lines)


__all__ = ["IperfCommandBuilder"]
