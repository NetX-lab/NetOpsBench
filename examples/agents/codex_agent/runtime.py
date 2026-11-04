"""Pinned diagnostic image and per-case, namespace-isolated container."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from netopsbench.platform.utils.files import atomic_write_json

CLI_VERSION = "0.159.2"
DEFAULT_IMAGE = f"netopsbench-codex:{CLI_VERSION}"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=120).strip()


def ensure_image(*, image=DEFAULT_IMAGE, binary=None, version=CLI_VERSION):
    try:
        image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "test",
            image,
            "-x",
            "/usr/local/bin/codex-code-mode-host",
        )
    except subprocess.CalledProcessError:
        binary = Path(binary or shutil.which("codex") or "").resolve()
        if not binary.is_file():
            raise RuntimeError("Install the pinned Codex CLI before building the diagnostic image") from None
        actual = subprocess.check_output([str(binary), "--version"], text=True).strip()
        if actual != f"codex-cli {version}":
            raise ValueError(f"Expected Codex {version}, found {actual}") from None
        with tempfile.TemporaryDirectory(prefix="netopsbench-codex-build-") as directory:
            source = Path(__file__).parent
            for name in ("Dockerfile", "entrypoint.sh"):
                shutil.copyfile(source / name, Path(directory) / name)
            shutil.copyfile(binary, Path(directory) / "codex")
            shutil.copyfile(binary.parent / "codex-code-mode-host", Path(directory) / "codex-code-mode-host")
            subprocess.run(["docker", "build", "-t", image, directory], check=True, timeout=600)
        image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    actual = docker("run", "--rm", "--network", "none", "--entrypoint", "codex", image, "--version")
    if actual != f"codex-cli {version}":
        raise ValueError(f"Diagnostic image CLI mismatch: {actual}")
    return image_id


def image_content_identity(image):
    """Compare executable content across Docker image-store implementations."""
    inspected = json.loads(docker("image", "inspect", image))[0]
    config = inspected.get("Config") or {}
    execution = {key: config.get(key) for key in ("Env", "Entrypoint", "Cmd", "Volumes", "Healthcheck")}
    execution.update({key: config.get(key) or "" for key in ("User", "WorkingDir", "StopSignal")})
    content = {key: inspected[key] for key in ("Architecture", "Os", "RootFS")}
    content["Config"] = execution
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def credentials(store: Path, *, refresh_from_host: bool = False) -> list[str]:
    """Seed only auth.json, never host configuration, skills or rollout history."""
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.chmod(0o700)
    target = store / "auth.json"
    source = Path.home() / ".codex" / "auth.json"
    if not target.exists():
        if not source.is_file():
            raise RuntimeError("Run `codex login` before Codex acceptance")
        shutil.copyfile(source, target)
    elif refresh_from_host and source.is_file() and source.resolve() != target.resolve():
        # Host login may have rotated the token since this dedicated store was seeded.
        host_auth = json.loads(source.read_text())
        private_auth = json.loads(target.read_text())

        def refreshed(auth):
            value = auth.get("last_refresh")
            return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.min.replace(tzinfo=UTC)

        if refreshed(host_auth) > refreshed(private_auth):
            atomic_write_json(target, host_auth)
    target.chmod(0o600)
    auth = json.loads(target.read_text())
    secrets = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and any(
                    marker in key.lower() for marker in ("token", "key", "account", "email")
                ):
                    secrets.append(item)
                else:
                    collect(item)

    collect(auth)
    return secrets


class DiagnosticContainer:
    def __init__(self, image, auth_store):
        self.image = image
        self.auth_store = auth_store.resolve()
        self.name = f"netopsbench-codex-{uuid.uuid4().hex[:12]}"
        self.network = self.name
        self.gateway = None

    def open(self):
        docker("network", "create", "--internal", "--label", "netopsbench.role=diagnostic", self.network)
        self.gateway = json.loads(docker("network", "inspect", self.network))[0]["IPAM"]["Config"][0]["Gateway"]
        return self

    def command(self, inputs, *, mcp_port, proxy_port, token, cli_args):
        return [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            self.name,
            "--label",
            "netopsbench.role=diagnostic",
            "--network",
            self.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETPCAP",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--tmpfs",
            "/workspace:rw,nosuid,size=512m",
            "--tmpfs",
            "/tmp:rw,nosuid,size=64m",
            "--mount",
            f"type=bind,src={inputs},dst=/input,readonly",
            "--mount",
            f"type=bind,src={self.auth_store},dst=/home/diagnostic/.codex",
            "-e",
            f"DIAGNOSTIC_UID={os.getuid()}",
            "-e",
            f"DIAGNOSTIC_GID={os.getgid()}",
            "-e",
            f"DIAGNOSTIC_GATEWAY={self.gateway}",
            "-e",
            f"DIAGNOSTIC_MCP_PORT={mcp_port}",
            "-e",
            f"DIAGNOSTIC_PROXY_PORT={proxy_port}",
            "-e",
            f"DIAGNOSTIC_MCP_TOKEN={token}",
            "-e",
            f"HTTPS_PROXY=http://{self.gateway}:{proxy_port}",
            "-e",
            f"HTTP_PROXY=http://{self.gateway}:{proxy_port}",
            "-e",
            f"NO_PROXY={self.gateway}",
            self.image,
            *cli_args,
        ]

    def close(self):
        errors = []
        for kind, name, removal in (
            ("container", self.name, ["docker", "rm", "-f", self.name]),
            ("network", self.network, ["docker", "network", "rm", self.network]),
        ):
            try:
                removed = subprocess.run(removal, capture_output=True, text=True, timeout=60)
                inspected = subprocess.run(
                    ["docker", "inspect", "--type", kind, name], capture_output=True, text=True, timeout=30
                )
                missing = inspected.returncode != 0 and any(
                    phrase in (inspected.stderr or "").lower() for phrase in ("no such", "not found")
                )
                if not missing:
                    detail = (removed.stderr or inspected.stderr or "resource still exists").strip()
                    errors.append(f"{kind} {name}: {detail}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{kind} {name}: {exc}")
        if errors:
            raise RuntimeError("Diagnostic cleanup failed: " + "; ".join(errors))
