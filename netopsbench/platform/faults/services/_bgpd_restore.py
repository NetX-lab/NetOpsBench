"""Reload only the stopped daemon's captured configuration inside its node."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path


def restore_bgpd():
    deadline = time.monotonic() + 25

    def run(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("BGP configuration recovery deadline exceeded")
        return subprocess.run(command, capture_output=True, text=True, timeout=remaining)

    status = run(["supervisorctl", "status", "bgpd"])
    if status.returncode not in (0, 3):
        raise RuntimeError(status.stderr or "Cannot read bgpd service state")
    if "RUNNING" not in status.stdout:
        started = run(["supervisorctl", "start", "bgpd"])
        if started.returncode:
            raise RuntimeError(started.stderr or started.stdout)
    with tempfile.TemporaryDirectory(prefix="netopsbench-bgpd-") as directory:
        path = Path(directory) / "bgpd.conf"
        path.write_text(sys.argv[1])
        restored = run(["vtysh", "-d", "bgpd", "-f", str(path)])
    if restored.returncode or any(line.startswith("%") for line in restored.stdout.splitlines()):
        raise RuntimeError(restored.stderr or restored.stdout or "Captured BGP configuration was not restored")


if __name__ == "__main__":
    restore_bgpd()
