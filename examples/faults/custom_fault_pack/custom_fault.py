"""Custom fault extension example — simplified with :func:`simple_fault`.

Shows the minimal steps to add a custom fault to NetOpsBench:

1. Write two plain functions: ``inject(ctx)`` and ``recover(ctx)``.
2. Call :func:`~netopsbench.sdk.simple_fault` to bundle them into a pack.
3. Pass the pack to ``bench.faults.register_pack(pack)`` before running.

This example injects real latency via ``tc netem`` inside Containerlab
containers.  It requires a running lab topology.

Usage::

    from netopsbench.sdk import NetOpsBench
    from examples.faults.custom_fault_pack import build_fault_pack

    bench = NetOpsBench(workspace=".")
    bench.faults.register_pack(build_fault_pack())

Or even shorter, without a pack object::

    bench.faults.register_fault("demo_custom_latency", inject, recover,
                                 requires_interface=True,
                                 required_parameters=("delay_ms",))
"""

from __future__ import annotations

import subprocess

from netopsbench.sdk import FaultContext, simple_fault

FAULT_NAME = "demo_custom_latency"


# ---------------------------------------------------------------------------
# Two plain functions — the only things a fault author needs to write
# ---------------------------------------------------------------------------

def inject(ctx: FaultContext) -> dict:
    """Apply tc-netem latency inside the target Containerlab container."""
    container = ctx.container_names.get(ctx.target_device) or f"clab-dcn-{ctx.target_device}"
    interface = ctx.target_interface or "Ethernet0"
    delay_ms = int(ctx.parameters.get("delay_ms", 25))
    jitter_ms = int(ctx.parameters.get("jitter_ms", 0))

    cmd = [
        "sudo", "docker", "exec", container,
        "tc", "qdisc", "replace", "dev", interface,
        "root", "netem", "delay", f"{delay_ms}ms",
    ]
    if jitter_ms > 0:
        cmd.append(f"{jitter_ms}ms")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {
        "success": result.returncode == 0,
        "target_device": ctx.target_device,
        "target_interface": interface,
        "delay_ms": delay_ms,
        "jitter_ms": jitter_ms,
        "command": " ".join(cmd),
        "error": result.stderr if result.returncode != 0 else None,
    }


def recover(ctx: FaultContext) -> dict:
    """Remove the tc-netem qdisc from the target interface."""
    container = ctx.container_names.get(ctx.target_device) or f"clab-dcn-{ctx.target_device}"
    interface = ctx.target_interface or "Ethernet0"

    cmd = ["sudo", "docker", "exec", container, "tc", "qdisc", "del", "dev", interface, "root"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {"success": True, "target_device": ctx.target_device, "target_interface": interface}


# ---------------------------------------------------------------------------
# Bundle into a pack (one line)
# ---------------------------------------------------------------------------

fault_pack = simple_fault(
    FAULT_NAME,
    inject=inject,
    recover=recover,
    requires_interface=True,
    required_parameters=("delay_ms",),
    aliases=["demo_custom_delay"],
)


def build_fault_pack():
    """Return the pre-built fault pack (backward-compatible factory)."""
    return fault_pack

