"""Internal lab lifecycle helpers for runtime provisioning."""

from __future__ import annotations

import os

from netopsbench.config import repo_root
from netopsbench.platform.topology.topology_utils import resolve_topology_dir
from netopsbench.platform.utils.proc import safe_run


def _repo_root() -> str:
    return str(repo_root())


_SCRIPT_LOCATIONS = {
    "deploy.sh": os.path.join("scripts", "runtime", "deploy.sh"),
    "teardown.sh": os.path.join("scripts", "runtime", "teardown.sh"),
}


def _script_path(name: str) -> str:
    rel_path = _SCRIPT_LOCATIONS.get(name, os.path.join("scripts", name))
    return os.path.join(_repo_root(), rel_path)


def resolve_generated_topology_dir(topology_dir: str | None, scale: str) -> str:
    base = topology_dir or "lab-topology"
    if base == "generated_topology":
        return f"lab-topology/generated_topology_{scale}"
    if any(base.endswith(f"generated_topology_{item}") for item in ("xs", "small", "medium", "large")):
        return base
    return os.path.join(base, f"generated_topology_{scale}")


def deploy_lab(
    scale: str,
    topology_dir: str | None,
    lab_name: str | None = None,
    mgmt_subnet: str | None = None,
    mgmt_network: str | None = None,
) -> int:
    script_path = _script_path("deploy.sh")
    if not os.path.exists(script_path):
        return 1
    topo_dir = topology_dir or "lab-topology"
    env = os.environ.copy()
    if lab_name:
        env["NETOPSBENCH_LAB_NAME"] = lab_name
    if mgmt_subnet:
        env["NETOPSBENCH_MGMT_SUBNET"] = mgmt_subnet
    if mgmt_network:
        env["NETOPSBENCH_MGMT_NETWORK"] = mgmt_network
    result = safe_run(
        ["bash", script_path, scale, topo_dir],
        check=False,
        env=env,
        timeout=1800,
    )
    return result.returncode


def _can_teardown_topology_dir(topo_dir: str) -> bool:
    has_topology_file = os.path.exists(os.path.join(topo_dir, "dcn.clab.yaml"))
    has_any_clab_file = (
        any(name.endswith((".clab.yaml", ".clab.yml")) for name in os.listdir(topo_dir))
        if os.path.isdir(topo_dir)
        else False
    )
    return has_topology_file or has_any_clab_file


def teardown_lab(topology_dir: str | None) -> int:
    topo_dir = resolve_topology_dir(topology_dir, "lab-topology")
    if not _can_teardown_topology_dir(topo_dir):
        return 1
    script_path = _script_path("teardown.sh")
    if not os.path.exists(script_path):
        return 1
    result = safe_run(["bash", script_path, topo_dir], check=False, timeout=600)
    return result.returncode
