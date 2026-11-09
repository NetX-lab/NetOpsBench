"""Read-only routing checks inside one Docker exec; no NetOpsBench imports."""

import argparse
import json
import subprocess
import time
from pathlib import Path


def packet_counters(path=Path("/proc/net/snmp")):
    """Read namespace-local counters; an unreadable counter is never zero."""
    try:
        lines = path.read_text().splitlines()
        values = {}
        for header, row in zip(lines[::2], lines[1::2], strict=True):
            names, counts = header.split(), row.split()
            if names[0] == counts[0] == "Icmp:":
                values = dict(zip(names[1:], map(int, counts[1:]), strict=True))
        return {"icmp_out_time_exceeded": values["OutTimeExcds"], "collected_at": time.time()}
    except (OSError, ValueError, KeyError, IndexError) as error:
        return {"error": type(error).__name__, "collected_at": time.time()}


def probe(run=subprocess.run, checks_to_run=None, budget_seconds=125):
    deadline = time.monotonic() + budget_seconds
    counters = packet_counters()
    checks = {}
    for name, command in (
        ("bgp", ["vtysh", "-c", "show ip bgp summary"]),
        ("interfaces", ["bash", "-lc", "show interfaces status"]),
        ("fib", ["ip", "-j", "-4", "route", "show"]),
    ):
        if checks_to_run is not None and name not in checks_to_run:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            checks[name] = {"returncode": -1, "stdout": "", "error": "deadline_exceeded"}
            continue
        try:
            result = run(command, capture_output=True, text=True, check=False, timeout=min(60, remaining))
            checks[name] = {"returncode": result.returncode, "stdout": result.stdout or ""}
        except (subprocess.TimeoutExpired, OSError) as error:
            checks[name] = {"returncode": -1, "stdout": "", "error": type(error).__name__}
    return {"version": 1, "checks": checks, "packet_counters": counters}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes-only", action="store_true")
    parser.add_argument("--budget-seconds", type=float, default=125)
    args = parser.parse_args()
    print(
        json.dumps(
            probe(checks_to_run=("bgp", "fib") if args.routes_only else None, budget_seconds=args.budget_seconds)
        )
    )
