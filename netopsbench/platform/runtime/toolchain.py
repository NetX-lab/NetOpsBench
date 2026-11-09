"""Resolve a checksum-verified official Containerlab release, without global fallback."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

ASSETS = Path(__file__).with_name("assets")
DEFAULT_TOOLS = Path(__file__).with_name("_bin")


def release_spec() -> dict:
    lock = json.loads((ASSETS / "containerlab.lock.json").read_text())
    architecture = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or architecture not in lock["releases"]:
        raise RuntimeError("The pinned runtime supports Linux amd64 and arm64 only")
    return {
        "provider": lock["provider"],
        "version": lock["version"],
        "architecture": architecture,
        **lock["releases"][architecture],
    }


def binary_version(binary: Path) -> str:
    result = subprocess.run([str(binary), "version", "--json"], capture_output=True, text=True, check=True, timeout=10)
    version = json.loads(result.stdout)["version"]
    if not isinstance(version, str):
        raise ValueError("Invalid Containerlab version response")
    return version


def runtime_tools() -> Path:
    root = Path(os.environ.get("NETOPSBENCH_RUNTIME_TOOLS", str(DEFAULT_TOOLS))).expanduser().resolve()
    hint = "Prepare the official runtime: python -m netopsbench.platform.runtime.build_tools"
    try:
        spec = release_spec()
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest != spec:
            raise ValueError("Runtime tools do not match the locked official release")
        binary = root / "containerlab"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Missing executable: containerlab")
        if hashlib.sha256(binary.read_bytes()).hexdigest() != spec["binary_sha256"]:
            raise ValueError("Containerlab executable checksum mismatch")
        if binary_version(binary) != spec["version"]:
            raise ValueError("Containerlab executable version mismatch")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{hint}. {exc}") from exc
    return root


def containerlab_command() -> list[str]:
    return [str(runtime_tools() / "containerlab")]


def runtime_backend_identity() -> dict[str, str]:
    """Freeze the actual executable and SONiC startup behavior in runtime/campaign identity."""
    root = runtime_tools()
    from netopsbench.platform.topology.config import SONIC_START_WRAPPER_SOURCE

    return {
        "provider": "upstream",
        "containerlab_version": binary_version(root / "containerlab"),
        "containerlab_sha256": hashlib.sha256((root / "containerlab").read_bytes()).hexdigest(),
        "sonic_start_sha256": hashlib.sha256(SONIC_START_WRAPPER_SOURCE.read_bytes()).hexdigest(),
    }
