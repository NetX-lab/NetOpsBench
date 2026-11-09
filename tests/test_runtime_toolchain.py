"""Official distribution integrity and backend identity are checked before allocation."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from netopsbench.platform.runtime import build_tools, resources, toolchain


@pytest.fixture
def release(tmp_path, monkeypatch):
    data = b'#!/bin/sh\nprintf \'{"version":"0.77.0"}\\n\'\n'
    archive = tmp_path / "official.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("containerlab")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    spec = dict(
        provider="upstream",
        version="0.77.0",
        architecture="amd64",
        archive_url="https://example.invalid",
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(data).hexdigest(),
    )
    monkeypatch.setattr(toolchain, "release_spec", lambda: dict(spec))
    monkeypatch.setattr(build_tools, "release_spec", lambda: dict(spec))
    output = tmp_path / "private-tools"
    monkeypatch.setenv("NETOPSBENCH_RUNTIME_TOOLS", str(output))
    return archive, output, spec


def test_install_and_resolve_official_release(release):
    archive, output, spec = release
    build_tools.install_release(archive, output)
    assert toolchain.runtime_tools() == output
    assert toolchain.containerlab_command() == [str(output / "containerlab")]
    assert json.loads((output / "manifest.json").read_text()) == spec
    assert sorted(p.name for p in output.iterdir()) == ["containerlab", "manifest.json"]


@pytest.mark.parametrize("tamper", ["archive", "executable", "manifest", "version", "permissions"])
def test_reject_corruption_before_use(release, monkeypatch, tamper):
    archive, output, _ = release
    if tamper == "archive":
        archive.write_bytes(b"not an archive")
        with pytest.raises(RuntimeError, match="archive checksum"):
            build_tools.install_release(archive, output)
        assert not output.exists()
        return
    build_tools.install_release(archive, output)
    if tamper == "executable":
        (output / "containerlab").write_bytes(b"corrupted")
    elif tamper == "manifest":
        (output / "manifest.json").write_text('{"provider":"custom"}')
    elif tamper == "version":
        monkeypatch.setattr(toolchain, "binary_version", lambda _: "0.76.0")
    else:
        (output / "containerlab").chmod(0o644)
    with pytest.raises(RuntimeError, match="official runtime"):
        toolchain.runtime_tools()


def test_missing_tools_have_no_global_fallback(release):
    with pytest.raises(RuntimeError, match="Prepare the official runtime"):
        toolchain.runtime_tools()


def test_reject_wrong_binary_even_with_valid_archive(release):
    archive, output, spec = release
    spec["binary_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="executable checksum"):
        build_tools.install_release(archive, output)
    assert not output.exists()


def test_backend_identity_hashes_executable_and_startup_script(release, monkeypatch, tmp_path):
    from netopsbench.platform.topology import config

    archive, output, spec = release
    build_tools.install_release(archive, output)
    wrapper = tmp_path / "start.sh"
    wrapper.write_text("first")
    monkeypatch.setattr(config, "SONIC_START_WRAPPER_SOURCE", wrapper)
    identity = toolchain.runtime_backend_identity()
    assert identity["containerlab_sha256"] == spec["binary_sha256"]
    assert identity["containerlab_version"] == "0.77.0"
    wrapper.write_text("changed")
    assert identity["sonic_start_sha256"] != toolchain.runtime_backend_identity()["sonic_start_sha256"]


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
def test_lock_selects_official_release_architecture(monkeypatch, architecture):
    monkeypatch.setattr(toolchain.platform, "system", lambda: "Linux")
    monkeypatch.setattr(toolchain.platform, "machine", lambda: architecture)
    spec = toolchain.release_spec()
    assert spec["provider"] == "upstream"
    assert spec["version"] == "0.77.0"
    assert spec["archive_url"].startswith("https://github.com/srl-labs/containerlab/releases/download/v0.77.0/")
    assert len(spec["binary_sha256"]) == 64


def test_capacity_rejects_old_or_mixed_backend_before_host_queries(monkeypatch):
    monkeypatch.setattr(resources, "runtime_backend_identity", lambda: {"provider": "upstream"})
    with pytest.raises(ValueError, match="measure the current runtime"):
        resources.capacity_check({"nodes": []}, "medium", Path("/nonexistent"))
    with pytest.raises(ValueError, match="same recorded runtime backend"):
        resources.peak_sample([{"nodes": []}, {"runtime_backend": {"provider": "upstream"}, "nodes": []}])


def test_runtime_records_identity_before_deploy_and_checks_cached_stage(monkeypatch, tmp_path):
    from netopsbench.platform.runtime import manager

    identity = {
        "provider": "upstream",
        "containerlab_version": "0.77.0",
        "containerlab_sha256": "a",
        "sonic_start_sha256": "b",
    }
    monkeypatch.setattr(manager, "runtime_backend_identity", lambda: dict(identity))
    runtime = manager.RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="identity")
    calls = []

    def deploy(_self, stage, pool):
        saved = json.loads((pool.root_dir / "runtime.json").read_text())
        assert saved["metadata"]["runtime_backend"] == identity
        calls.append(stage)
        from datetime import UTC, datetime

        from netopsbench.platform.runtime.lifecycle import LifecycleStageResult

        return LifecycleStageResult(
            stage=stage,
            status="completed",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            duration_seconds=0,
        )

    monkeypatch.setattr(manager.RuntimeLifecycle, "run", deploy)
    runtime.deploy().deploy()
    assert calls == ["deploy"]
    identity["sonic_start_sha256"] = "changed"
    with pytest.raises(ValueError, match="backend changed"):
        runtime.deploy()
    assert calls == ["deploy"]


def test_cancelled_deploy_does_not_resolve_tools_or_create_topology(monkeypatch, tmp_path):
    import threading

    from netopsbench.models.runtime import RuntimeIdentity
    from netopsbench.platform.runtime import deployment
    from netopsbench.platform.utils.control import OperationCancelled, OperationControl

    worker = RuntimeIdentity.create(
        runtime_id="cancel",
        worker_id="worker-1",
        worker_index=1,
        lab_name="cancel",
        topology_dir=tmp_path / "absent",
        mgmt_subnet="172.31.1.0/24",
        mgmt_network="cancel",
    )
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(deployment, "containerlab_command", lambda: pytest.fail("resolved tools after cancellation"))
    with pytest.raises(OperationCancelled):
        deployment.deploy_worker_lab(worker, "xs", control=OperationControl(cancel))
    assert not worker.topology_dir.exists()


def test_failed_destroy_is_reported_after_remaining_cleanup(monkeypatch, tmp_path):
    import subprocess

    from netopsbench.models.runtime import RuntimeIdentity
    from netopsbench.platform.runtime import deployment

    worker = RuntimeIdentity.create(
        runtime_id="cleanup",
        worker_id="worker-1",
        worker_index=1,
        lab_name="cleanup",
        topology_dir=tmp_path,
        mgmt_subnet="172.31.1.0/24",
        mgmt_network="owned",
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[0] == "official" else 0, "", "namespace failed")

    monkeypatch.setattr(deployment, "containerlab_command", lambda: ["official"])
    monkeypatch.setattr(deployment, "safe_run", run)
    monkeypatch.setattr(deployment, "sudo_prefix", lambda: [])
    monkeypatch.setattr(deployment, "docker_prefix", lambda: [])
    monkeypatch.setattr(deployment, "_stop_collector", lambda *a: None)
    monkeypatch.setattr(deployment, "_lab_container_names", lambda *a: [])
    monkeypatch.setattr(deployment, "_container_exists", lambda *a: False)
    monkeypatch.setattr(deployment, "_network_exists", lambda *a: False)
    with pytest.raises(RuntimeError, match="namespace failed"):
        deployment.teardown_worker_lab(worker)
    assert ["docker", "network", "rm", "owned"] in calls
    assert len([c for c in calls if c[0] == "official"]) == 2
