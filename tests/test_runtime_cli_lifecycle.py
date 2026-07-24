from __future__ import annotations

import pytest

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime import lifecycle


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
