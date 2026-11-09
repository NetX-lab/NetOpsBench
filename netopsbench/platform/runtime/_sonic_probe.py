"""Standalone, read-only SONiC probe executed inside one Docker exec.

Keep this module standard-library-only: its source is sent to the container,
where NetOpsBench is not installed. Preserve the activation checks' ordering.
"""

import json
import subprocess
import sys


def probe(services, expected_port_count=None, run=subprocess.run):
    checks = {}

    def command(name, args):
        try:
            result = run(args, capture_output=True, text=True, check=False, timeout=15)
        except (subprocess.TimeoutExpired, OSError) as error:
            checks[name] = {"ok": False, "error": type(error).__name__}
            return None
        checks[name] = {"ok": result.returncode == 0}
        return result

    def cfg(name):
        result = command(name, ["sonic-cfggen", "-d", "-v", "DEVICE_METADATA.localhost.hostname"])
        return result is not None and result.returncode == 0

    ready = False
    if cfg("config_before"):
        version = command("vtysh", ["vtysh", "-c", "show version"])
        if version is not None and version.returncode == 0:
            status = command("services", ["supervisorctl", "status", *services])
            states = {}
            if status is not None:
                for line in (status.stdout or status.stderr or "").splitlines():
                    fields = line.split()
                    if len(fields) >= 2:
                        states[fields[0]] = fields[1].upper()
            checks["services"]["ok"] = all(states.get(service) == "RUNNING" for service in services)
            if checks["services"]["ok"] and cfg("config_after"):
                ready = True
                if expected_port_count is not None:
                    ports = command("ports", ["redis-cli", "-n", "2", "--raw", "HLEN", "COUNTERS_PORT_NAME_MAP"])
                    try:
                        ready = (
                            ports is not None
                            and ports.returncode == 0
                            and int(ports.stdout.strip()) >= expected_port_count
                        )
                    except (ValueError, AttributeError):
                        ready = False
                    checks["ports"]["ok"] = ready
    return {"version": 1, "ready": ready, "checks": checks}


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    print(json.dumps(probe(request["services"], request.get("expected_port_count"))))
