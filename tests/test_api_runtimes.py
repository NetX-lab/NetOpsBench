"""Tests for the public runtime pool SDK manager."""


def test_runtime_manager_create_workers_1_returns_runtime_pool(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimePool

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="small", workers=1)

    assert isinstance(runtime, RuntimePool)
    assert runtime.id == runtime.name
    assert runtime.scale == "small"
    assert len(runtime.workers) == 1
    assert runtime.workers[0].name == "worker-1"
    assert runtime.workers[0].index == 1
    assert runtime.workers[0].lab_name == runtime.name
    assert runtime.workers[0].topology_dir == str(runtime.root_dir / "worker-1")
    assert runtime.root_dir.exists()
    assert runtime.root_dir == tmp_path / ".netopsbench" / "runtimes" / runtime.name
    assert runtime.status() == {"id": runtime.id, "name": runtime.name, "scale": "small", "state": "created"}


def test_runtime_manager_attach_list_get_roundtrip(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")

    attached_manager = RuntimeManager(workspace=tmp_path)
    attached = attached_manager.attach(runtime.root_dir)

    assert attached.name == "runtime-xs"
    assert attached.id == "runtime-xs"
    assert attached.workers[0].root_dir == runtime.root_dir / "worker-1"
    assert attached.workers[0].topology_dir == str(runtime.root_dir / "worker-1")
    assert attached.status()["state"] == "created"
    assert attached_manager.get("runtime-xs").root_dir == runtime.root_dir
    assert [item.name for item in attached_manager.list()] == ["runtime-xs"]


def test_runtime_pool_exposes_required_lifecycle_surface(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    runtime = RuntimeManager(workspace=tmp_path).create(scale="medium", workers=1, name="runtime-medium")

    deployed = runtime.deploy()
    observed = runtime.ensure_observability()
    pingmesh_ready = runtime.ensure_pingmesh()
    warmed = runtime.warm()

    assert deployed is runtime
    assert observed is runtime
    assert pingmesh_ready is runtime
    assert warmed is runtime
    assert callable(runtime.status)
    assert callable(runtime.teardown)
    assert runtime.status()["state"] == "warm"

    torn_down = runtime.teardown()

    assert torn_down is runtime
    assert runtime.status()["state"] == "torn_down"
    assert not runtime.root_dir.exists()


def test_runtime_manager_attach_rejects_missing_runtime_metadata(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "broken-runtime"
    runtime_dir.mkdir(parents=True)

    manager = RuntimeManager(workspace=tmp_path)

    try:
        manager.attach(runtime_dir)
    except FileNotFoundError as exc:
        assert "runtime.json" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError for missing runtime metadata")


def test_runtime_manager_attach_rejects_malformed_runtime_metadata(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "broken-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.json").write_text('{"name":"broken-runtime"}', encoding="utf-8")

    manager = RuntimeManager(workspace=tmp_path)

    try:
        manager.attach(runtime_dir)
    except ValueError as exc:
        assert "missing required runtime metadata" in str(exc)
    else:
        raise AssertionError("expected ValueError for malformed runtime metadata")


def test_runtime_manager_provision_single_worker_uses_worker_pool_deploy_hook(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    captured = {}

    def fake_deploy_workers(workers, scale, repo_root, observability_core_ready=False):
        captured["scale"] = scale
        captured["worker_count"] = len(workers)
        captured["topology_dir"] = workers[0].topology_dir
        captured["lab_name"] = workers[0].lab_name
        captured["mgmt_subnet"] = workers[0].mgmt_subnet

    monkeypatch.setattr(runtimes_mod, "deploy_workers", fake_deploy_workers)

    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime = manager.provision(scale="xs", workers=1, name="runtime-xs")

    assert runtime.state == "deployed"
    assert runtime.metadata["provisioning_mode"] == "worker_pool"
    assert captured["scale"] == "xs"
    assert captured["lab_name"] == "runtime-xs"
    assert captured["topology_dir"] == str(runtime.root_dir / "worker-1")
    assert runtime.workers[0].topology_dir == str(runtime.root_dir / "worker-1")


def test_runtime_pool_teardown_uses_worker_pool_teardown_hook(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    runtime = runtimes_mod.RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="runtime-xs")
    runtime.metadata["provisioning_mode"] = "worker_pool"
    runtime.state = "deployed"
    runtime._write_metadata()
    (runtime.root_dir / "worker-1" / "topology.json").write_text('{"name":"runtime-xs"}', encoding="utf-8")

    called = {}

    def fake_teardown_workers(workers, repo_root):
        called["worker_count"] = len(workers)
        called["topology_dir"] = workers[0].topology_dir

    monkeypatch.setattr(runtimes_mod, "teardown_workers", fake_teardown_workers)

    runtime.teardown()

    assert called["topology_dir"] == str(runtime.root_dir / "worker-1")
    assert called["worker_count"] == 1
    assert runtime.state == "torn_down"
    assert not runtime.root_dir.exists()


def test_provision_cleans_up_metadata_on_deploy_failure(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    def failing_deploy_workers(workers, scale, repo_root, observability_core_ready=False):
        raise RuntimeError("deploy failed")

    monkeypatch.setattr(runtimes_mod, "deploy_workers", failing_deploy_workers)

    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "will-fail"

    try:
        manager.provision(scale="xs", workers=1, name="will-fail")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError from deploy")

    assert not runtime_dir.exists(), "stale runtime directory should be cleaned up on provision failure"
    assert manager.list() == [], "no stale runtimes should remain after provision failure"
