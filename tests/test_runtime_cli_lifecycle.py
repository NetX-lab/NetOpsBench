from __future__ import annotations

from contextlib import nullcontext

import pytest

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime import deployment, lifecycle
from netopsbench.platform.topology.generator import generate_topology


@pytest.fixture(autouse=True)
def _isolate_runtime_slot(monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_deploy_lock", nullcontext)
    monkeypatch.setattr(lifecycle, "assert_worker_slot_available", lambda _worker: None)


def _worker(tmp_path) -> RuntimeIdentity:
    return RuntimeIdentity.create(
        runtime_id="runtime-xs",
        worker_id="worker-1",
        worker_index=1,
        lab_name="runtime-xs",
        topology_dir=tmp_path,
        mgmt_subnet="172.31.1.0/24",
        mgmt_network="clab-mgmt-runtime-xs",
    )


@pytest.mark.parametrize("failed_stage", ["deploy", "observability", "client_agent", "health"])
def test_standalone_worker_deploy_compensates_every_failed_stage(tmp_path, monkeypatch, failed_stage):
    calls: list[str] = []

    def stage(name):
        def run(*_args, **_kwargs):
            calls.append(name)
            if name == failed_stage:
                raise RuntimeError(f"{name} failed")

        return run

    monkeypatch.setattr(lifecycle, "deploy_worker_lab", stage("deploy"))
    monkeypatch.setattr(lifecycle, "ensure_worker_observability", stage("observability"))
    monkeypatch.setattr(lifecycle, "ensure_worker_client_agent", stage("client_agent"))
    monkeypatch.setattr(lifecycle, "validate_worker_health", stage("health"))
    monkeypatch.setattr(lifecycle, "teardown_worker_lab", stage("teardown"))

    with pytest.raises(RuntimeError, match=f"{failed_stage} failed"):
        lifecycle.deploy_worker_transactionally(_worker(tmp_path), "xs")

    assert calls[-1] == "teardown"


def test_standalone_worker_deploy_preserves_provision_and_cleanup_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "deploy_worker_lab", lambda *_args: (_ for _ in ()).throw(ValueError("deploy")))
    monkeypatch.setattr(lifecycle, "teardown_worker_lab", lambda *_args: (_ for _ in ()).throw(OSError("cleanup")))

    with pytest.raises(RuntimeError, match=r"ValueError: deploy.*OSError: cleanup"):
        lifecycle.deploy_worker_transactionally(_worker(tmp_path), "xs")


def test_standalone_worker_preflight_conflict_does_not_teardown_existing_lab(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "assert_worker_slot_available",
        lambda _worker: (_ for _ in ()).throw(RuntimeError("lab already exists")),
    )
    monkeypatch.setattr(
        lifecycle,
        "teardown_worker_lab",
        lambda *_args: pytest.fail("preflight failure must not teardown the existing lab"),
    )

    with pytest.raises(RuntimeError, match="lab already exists"):
        lifecycle.deploy_worker_transactionally(_worker(tmp_path), "xs")


def test_sonic_pid1_deployment_contract_accepts_signal_handling_wrapper(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    generate_topology("xs", str(tmp_path), name=worker.lab_name)
    containers = [
        f"clab-{worker.lab_name}-spine1",
        f"clab-{worker.lab_name}-spine2",
        f"clab-{worker.lab_name}-leaf1",
        f"clab-{worker.lab_name}-leaf2",
    ]
    stdout = "\n".join(
        f"""/{name}\ttrue\t{100 + index}\t["-c","trap 'exit 0' TERM INT; sleep infinity & wait $!"]"""
        for index, name in enumerate(containers)
    )
    monkeypatch.setattr(
        deployment,
        "safe_run",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 0, "stdout": stdout, "stderr": ""},
        )(),
    )
    monkeypatch.setattr(deployment, "_read_process_comm", lambda _pid: "bash")

    deployment._verify_sonic_pid1_contract(worker)


def test_sonic_pid1_deployment_contract_rejects_interactive_bash(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    generate_topology("xs", str(tmp_path), name=worker.lab_name)
    stdout = "\n".join(
        f"/clab-{worker.lab_name}-{name}\ttrue\t10\t[]" for name in ("spine1", "spine2", "leaf1", "leaf2")
    )
    monkeypatch.setattr(
        deployment,
        "safe_run",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 0, "stdout": stdout, "stderr": ""},
        )(),
    )
    monkeypatch.setattr(deployment, "_read_process_comm", lambda _pid: "bash")

    with pytest.raises(RuntimeError, match="PID 1 contract failed"):
        deployment._verify_sonic_pid1_contract(worker)
