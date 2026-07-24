"""Tests for the public runtime pool SDK manager."""

import json
from contextlib import nullcontext
from pathlib import Path

import pytest


def _record_lifecycle_operations(monkeypatch, calls, *, fail_stage=None):
    import netopsbench.platform.runtime.lifecycle as lifecycle

    def record(stage, runtime_id):
        calls.append((stage, runtime_id))
        if stage == fail_stage:
            raise RuntimeError(f"{stage} failed")

    monkeypatch.setattr(lifecycle, "runtime_deploy_lock", nullcontext)
    monkeypatch.setattr(
        lifecycle,
        "allocate_management_subnets",
        lambda _scale, count, _registry: [f"172.31.{100 + index}.0/24" for index in range(count)],
    )
    monkeypatch.setattr(
        lifecycle,
        "deploy_workers",
        lambda workers, _scale, _root, _registry: record("deploy", workers[0].runtime_id),
    )
    monkeypatch.setattr(
        lifecycle,
        "ensure_worker_observability",
        lambda worker, **_kwargs: record("observability", worker.runtime_id),
    )
    monkeypatch.setattr(
        lifecycle,
        "ensure_worker_client_agent",
        lambda worker: record("pingmesh", worker.runtime_id),
    )
    monkeypatch.setattr(
        lifecycle,
        "validate_worker_health",
        lambda worker, _root, _registry: record("warm", worker.runtime_id),
    )
    monkeypatch.setattr(
        lifecycle,
        "teardown_workers",
        lambda workers, _registry: record("teardown", workers[0].runtime_id),
    )


def test_runtime_manager_create_workers_1_returns_runtime_pool(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimePool

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="small", workers=1)

    assert isinstance(runtime, RuntimePool)
    assert runtime.id == runtime.name
    assert runtime.scale == "small"
    assert len(runtime.workers) == 1
    assert runtime.workers[0].worker_id == "worker-1"
    assert runtime.workers[0].worker_index == 1
    assert runtime.workers[0].lab_name == runtime.name
    assert runtime.workers[0].topology_dir == runtime.root_dir / "worker-1"
    assert runtime.root_dir.exists()
    assert runtime.root_dir == tmp_path / ".netopsbench" / "runtimes" / runtime.name
    assert runtime.status() == {"id": runtime.id, "name": runtime.name, "scale": "small", "state": "created"}


def test_xlarge_runtime_workers_use_non_overlapping_23_subnets(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    runtime = RuntimeManager(workspace=tmp_path).create(scale="xlarge", workers=2, name="runtime-xlarge")

    subnets = [worker.mgmt_subnet for worker in runtime.workers]
    assert subnets[0].endswith("/23")
    assert subnets[1].endswith("/23")
    assert subnets[0] != subnets[1]
    assert int(subnets[0].split(".")[2]) % 2 == 0
    assert int(subnets[1].split(".")[2]) == int(subnets[0].split(".")[2]) + 2


def test_fat_tree_k12_runtime_workers_use_non_overlapping_23_subnets(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    runtime = RuntimeManager(workspace=tmp_path).create(scale="fat-tree-k12", workers=2, name="runtime-ft12")

    subnets = [worker.mgmt_subnet for worker in runtime.workers]
    assert subnets[0].endswith("/23")
    assert subnets[1].endswith("/23")
    assert subnets[0] != subnets[1]
    assert int(subnets[0].split(".")[2]) >= 220
    assert int(subnets[0].split(".")[2]) % 2 == 0
    assert int(subnets[1].split(".")[2]) == int(subnets[0].split(".")[2]) + 2


def test_runtime_manager_attach_list_get_roundtrip(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")

    attached_manager = RuntimeManager(workspace=tmp_path)
    attached = attached_manager.attach(runtime.root_dir)

    assert attached.name == "runtime-xs"
    assert attached.id == "runtime-xs"
    assert attached.workers[0].topology_dir == runtime.root_dir / "worker-1"
    assert attached.status()["state"] == "created"
    assert attached_manager.get("runtime-xs").root_dir == runtime.root_dir
    assert [item.name for item in attached_manager.list()] == ["runtime-xs"]


def test_runtime_attach_rejects_tampered_resolved_scale_profile(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimeProvisionError

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")
    metadata_path = runtime.root_dir / "runtime.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["resolved_scale_profile"]["traffic"]["max_pps_per_client"] = 999
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeProvisionError, match="does not match"):
        manager.attach(runtime.root_dir)


def test_same_scale_runtime_identities_are_isolated_and_persisted(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    manager = RuntimeManager(workspace=tmp_path)
    first = manager.create(scale="xlarge", workers=1, name="run-a")
    second = manager.create(scale="xlarge", workers=1, name="run-b")

    first_identity = first.workers[0]
    second_identity = second.workers[0]
    assert first_identity.topology_id == "run-a"
    assert second_identity.topology_id == "run-b"
    assert first_identity.bucket == "network_data_run-a_w01"
    assert second_identity.bucket == "network_data_run-b_w01"
    assert first_identity.bucket != second_identity.bucket
    assert first_identity.mgmt_network == "clab-mgmt-run-a"

    payload = json.loads((first.root_dir / "runtime.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "3"
    assert payload["scale_registry_sha256"] == manager.scale_registry.digest
    assert payload["scale_profile_sha256"] == manager.scale_registry.get("xlarge").digest
    assert payload["resolved_scale_profile"]["name"] == "xlarge"
    assert payload["workers"][0] == first_identity.model_dump(mode="json")


def test_runtime_identity_has_no_process_environment_projection(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="identity-lab").workers[0]
    assert worker.topology_id == "identity-lab"
    assert worker.bucket == "network_data_identity-lab_w01"
    assert worker.mgmt_network == "clab-mgmt-identity-lab"
    assert not hasattr(worker, "as_env")


def test_worker_observability_restarts_stale_bgp_collector(tmp_path, monkeypatch):
    from netopsbench.platform.observability import lifecycle
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="collector-lab").workers[0]
    topology_dir = Path(worker.topology_dir)
    (topology_dir / "topology.json").write_text("{}", encoding="utf-8")
    (topology_dir / "bgp_collector.pid").write_text("999999\n", encoding="utf-8")
    started = []

    class Process:
        pid = 4242

    monkeypatch.setattr(lifecycle, "_bgp_collector_is_running", lambda *_args: False)
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda command, **kwargs: started.append((command, kwargs)) or Process(),
    )

    lifecycle.ensure_worker_bgp_collector(worker)

    assert (topology_dir / "bgp_neighbors.lp").is_file()
    assert (topology_dir / "bgp_collector.pid").read_text(encoding="utf-8") == "4242\n"
    assert "netopsbench.platform.observability.bgp_collector" in started[0][0]
    assert str(topology_dir / "topology.json") in started[0][0]
    interval_index = started[0][0].index("--interval")
    assert started[0][0][interval_index + 1] == "10.0"
    bucket_index = started[0][0].index("--influxdb-bucket")
    assert started[0][0][bucket_index + 1] == worker.bucket


def test_worker_telegraf_exposes_pingmesh_ingest_before_returning(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.observability import lifecycle
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="relay-lab").workers[0]
    (Path(worker.topology_dir) / "topology.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_update(_topology_file, *, output_file, **_kwargs):
        Path(output_file).write_text("[agent]\n", encoding="utf-8")

    def fake_safe_run(command, **kwargs):
        calls.append(("command", [str(part) for part in command], kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lifecycle, "update_telegraf_config", fake_update)
    monkeypatch.setattr(lifecycle, "safe_run", fake_safe_run)
    monkeypatch.setattr(lifecycle, "_collector_ip", lambda _topology_file: "172.31.101.2")
    monkeypatch.setattr(
        lifecycle,
        "_wait_for_telegraf_listener",
        lambda topology_file: calls.append(("ready", Path(topology_file))),
    )

    lifecycle.ensure_worker_telegraf(worker)

    run_command = next(command for kind, command, _kwargs in calls if kind == "command" and "run" in command)
    alias_index = run_command.index("--network-alias")
    assert run_command[alias_index + 1] == "telegraf"
    assert calls[-1] == ("ready", Path(worker.topology_dir) / "topology.json")


def test_python_worker_deploy_owns_topology_containerlab_and_activation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import deployment
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="deploy-lab").workers[0]
    topology_dir = Path(worker.topology_dir)
    calls = []

    def fake_generate_topology(**kwargs):
        calls.append(("generate", kwargs))
        (topology_dir / "deploy-lab.clab.yaml").write_text("name: deploy-lab\n", encoding="utf-8")

    def fake_safe_run(command, **kwargs):
        calls.append(("command", [str(part) for part in command], kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deployment, "generate_topology", fake_generate_topology)
    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)
    monkeypatch.setattr(deployment, "apply_configs", lambda *args: SimpleNamespace(failed=[]))
    deployment.deploy_worker_lab(worker, "xs")

    generated = next(item for item in calls if item[0] == "generate")[1]
    commands = [item[1] for item in calls if item[0] == "command"]
    assert generated["name"] == "deploy-lab"
    assert generated["mgmt_subnet"] == worker.mgmt_subnet
    assert any("containerlab" in command and "deploy" in command for command in commands)
    assert not any("telegraf" in " ".join(command) or "pingmesh" in " ".join(command) for command in commands)


def test_k12_worker_deploy_uses_profile_containerlab_parallelism(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import deployment
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="fat-tree-k12", workers=1, name="deploy-k12").workers[0]
    topology_dir = Path(worker.topology_dir)
    commands = []

    def fake_generate_topology(**_kwargs):
        topology_dir.mkdir(parents=True, exist_ok=True)
        (topology_dir / "deploy-k12.clab.yaml").write_text("name: deploy-k12\n", encoding="utf-8")

    def fake_safe_run(command, **_kwargs):
        commands.append([str(part) for part in command])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deployment, "generate_topology", fake_generate_topology)
    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)
    monkeypatch.setattr(deployment, "apply_configs", lambda *args: SimpleNamespace(failed=[]))
    deployment.deploy_worker_lab(worker, "fat-tree-k12")

    deploy_command = next(command for command in commands if "containerlab" in command and "deploy" in command)
    assert deploy_command[-2:] == ["--max-workers", "1"]


def test_removed_containerlab_env_does_not_override_scale_profile(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import deployment
    from netopsbench.sdk.runtimes import RuntimeManager

    worker = RuntimeManager(workspace=tmp_path).create(scale="fat-tree-k12", workers=1, name="deploy-k12").workers[0]
    topology_dir = Path(worker.topology_dir)
    commands = []

    def fake_generate_topology(**_kwargs):
        topology_dir.mkdir(parents=True, exist_ok=True)
        (topology_dir / "deploy-k12.clab.yaml").write_text("name: deploy-k12\n", encoding="utf-8")

    def fake_safe_run(command, **_kwargs):
        commands.append([str(part) for part in command])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deployment, "generate_topology", fake_generate_topology)
    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)
    monkeypatch.setattr(deployment, "apply_configs", lambda *args: SimpleNamespace(failed=[]))
    monkeypatch.setenv("NETOPSBENCH_CONTAINERLAB_MAX_WORKERS", "3")

    deployment.deploy_worker_lab(worker, "fat-tree-k12")

    deploy_command = next(command for command in commands if "containerlab" in command and "deploy" in command)
    assert deploy_command[-2:] == ["--max-workers", "1"]


def test_management_subnet_allocation_batches_docker_inspection(monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import deployment

    commands = []

    def fake_safe_run(command, **_kwargs):
        commands.append([str(part) for part in command])
        if "ls" in command:
            return SimpleNamespace(returncode=0, stdout="network-a\nnetwork-b\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="172.31.101.0/24\n10.0.0.0/8\n", stderr="")

    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)

    assert deployment.allocate_management_subnets("xs", 2) == ["172.31.102.0/24", "172.31.103.0/24"]
    assert len(commands) == 2
    inspect_command = commands[1]
    assert inspect_command[-2:] == ["network-a", "network-b"]


@pytest.mark.parametrize("failed_operation", ["ls", "inspect"])
def test_management_subnet_allocation_fails_when_docker_query_fails(monkeypatch, failed_operation):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import deployment

    def fake_safe_run(command, **_kwargs):
        operation = "ls" if "ls" in command else "inspect"
        if operation == failed_operation:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"{operation} failed")
        return SimpleNamespace(returncode=0, stdout="network-a\n", stderr="")

    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)

    verb = "list" if failed_operation == "ls" else "inspect"
    with pytest.raises(RuntimeError, match=f"Unable to {verb} Docker networks"):
        deployment.allocate_management_subnets("xs", 1)


def test_runtime_identity_resolves_relative_workspace_paths(tmp_path, monkeypatch):
    from netopsbench.sdk.runtimes import RuntimeManager

    monkeypatch.chdir(tmp_path)

    runtime = RuntimeManager(workspace=".").create(scale="xs", workers=1, name="absolute-path-lab")

    assert Path(runtime.workers[0].topology_dir).is_absolute()
    assert runtime.workers[0].topology_dir == (
        tmp_path / ".netopsbench" / "runtimes" / "absolute-path-lab" / "worker-1"
    )


def test_teardown_worker_identity_uses_manifest_management_network(tmp_path):
    from netopsbench.platform.runtime.deployment import worker_from_topology
    from netopsbench.platform.topology.generator import generate_topology

    topology_dir = tmp_path / "topology"
    generate_topology(
        "xs",
        str(topology_dir),
        name="custom-lab",
        mgmt_subnet="172.30.40.0/24",
        mgmt_network="custom-management",
    )

    worker = worker_from_topology(str(topology_dir))

    assert worker.lab_name == "custom-lab"
    assert worker.mgmt_subnet == "172.30.40.0/24"
    assert worker.mgmt_network == "custom-management"
    assert worker.bucket == "network_data_custom-lab_w01"
    assert worker.topology_dir == topology_dir.resolve()


def test_teardown_removal_barrier_waits_until_docker_container_set_is_empty(monkeypatch):
    from netopsbench.platform.runtime import deployment

    observations = [["clab-test-leaf1"], ["clab-test-leaf1"], []]
    sleeps = []
    removals = []
    monkeypatch.setattr(deployment, "_lab_container_names", lambda *_args: observations.pop(0))
    monkeypatch.setattr(deployment, "safe_run", lambda command, **_kwargs: removals.append(command))
    monkeypatch.setattr(deployment.time, "sleep", sleeps.append)

    deployment._wait_for_lab_removal([], "test")

    assert sleeps == [deployment.LAB_REMOVAL_POLL_SECONDS] * 2
    assert removals == [["docker", "rm", "-f", "clab-test-leaf1"]] * 2


def test_runtime_pool_exposes_required_lifecycle_surface(tmp_path, monkeypatch):
    from netopsbench.sdk.runtimes import RuntimeManager

    calls = []
    _record_lifecycle_operations(monkeypatch, calls)
    runtime = RuntimeManager(workspace=tmp_path).create(scale="medium", workers=1, name="runtime-medium")

    deployed = runtime.deploy()
    observed = runtime.ensure_observability()
    pingmesh_ready = runtime.ensure_pingmesh()
    warmed = runtime.warm()
    runtime.warm()

    assert deployed is runtime
    assert observed is runtime
    assert pingmesh_ready is runtime
    assert warmed is runtime
    assert callable(runtime.status)
    assert callable(runtime.teardown)
    assert runtime.status()["state"] == "warm"
    assert [stage for stage, _ in calls] == [
        "deploy",
        "observability",
        "pingmesh",
        "warm",
    ]
    assert set(runtime.stage_results) == {
        "deploy",
        "observability",
        "pingmesh",
        "warm",
    }

    torn_down = runtime.teardown()

    assert torn_down is runtime
    assert runtime.status()["state"] == "torn_down"
    assert not runtime.root_dir.exists()
    assert [stage for stage, _ in calls][-1] == "teardown"


def test_runtime_stage_failure_is_persisted_without_advancing_state(tmp_path, monkeypatch):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimeProvisionError

    calls = []
    _record_lifecycle_operations(monkeypatch, calls, fail_stage="observability")
    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-failure")
    runtime.deploy()

    with pytest.raises(RuntimeProvisionError, match="observability"):
        runtime.ensure_observability()

    assert runtime.state == "deployed"
    assert runtime.stage_results["observability"].status == "failed"
    attached = manager.attach(runtime.root_dir)
    assert attached.stage_results["observability"].error == "RuntimeError: observability failed"


def test_runtime_manager_attach_rejects_missing_runtime_metadata(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimeProvisionError

    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "broken-runtime"
    runtime_dir.mkdir(parents=True)

    manager = RuntimeManager(workspace=tmp_path)

    try:
        manager.attach(runtime_dir)
    except RuntimeProvisionError as exc:
        assert "runtime.json" in str(exc)
    else:
        raise AssertionError("expected RuntimeProvisionError for missing runtime metadata")


def test_runtime_manager_attach_rejects_malformed_runtime_metadata(tmp_path):
    from netopsbench.sdk.runtimes import RuntimeManager, RuntimeProvisionError

    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "broken-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.json").write_text('{"name":"broken-runtime"}', encoding="utf-8")

    manager = RuntimeManager(workspace=tmp_path)

    try:
        manager.attach(runtime_dir)
    except RuntimeProvisionError as exc:
        assert "Unsupported runtime.json schema" in str(exc)
    else:
        raise AssertionError("expected RuntimeProvisionError for malformed runtime metadata")


def test_runtime_manager_provision_composes_lifecycle_stages(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    calls = []
    _record_lifecycle_operations(monkeypatch, calls)
    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime = manager.provision(scale="xs", workers=1, name="runtime-xs")

    assert runtime.state == "warm"
    assert runtime.metadata["provisioning_mode"] == "worker_pool"
    assert [stage for stage, _ in calls] == [
        "deploy",
        "observability",
        "pingmesh",
        "warm",
    ]
    assert runtime.workers[0].topology_id == "runtime-xs"
    assert runtime.workers[0].topology_dir == runtime.root_dir / "worker-1"


def test_observability_stage_records_only_confirmed_created_bucket(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.lifecycle as lifecycle
    from netopsbench.platform.runtime.manager import RuntimeManager

    manager = RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")

    def fake_ensure(worker, *, on_bucket_created):
        on_bucket_created(worker.bucket)

    monkeypatch.setattr(lifecycle, "ensure_worker_observability", fake_ensure)
    runtime.ensure_observability()

    payload = json.loads(manager.telemetry_ownership_file.read_text())
    assert set(payload["buckets"]) == {runtime.workers[0].bucket}


def test_runtime_pool_teardown_uses_worker_pool_teardown_hook(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod
    from netopsbench.platform.observability.ownership import ManagedBucketRegistry

    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")
    registry = ManagedBucketRegistry(manager.telemetry_ownership_file)
    registry.record_created(runtime.workers[0].bucket, runtime.id)
    runtime.metadata["provisioning_mode"] = "worker_pool"
    runtime.state = "deployed"
    runtime._write_metadata()
    (runtime.root_dir / "worker-1" / "topology.json").write_text('{"name":"runtime-xs"}', encoding="utf-8")

    calls = []
    _record_lifecycle_operations(monkeypatch, calls)

    runtime.teardown()

    assert calls == [("teardown", "runtime-xs")]
    assert runtime.state == "torn_down"
    assert not runtime.root_dir.exists()
    assert json.loads(manager.telemetry_ownership_file.read_text())["buckets"][runtime.workers[0].bucket]["retired_at"]


def test_failed_physical_teardown_does_not_retire_owned_bucket(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod
    from netopsbench.platform.observability.ownership import ManagedBucketRegistry

    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime = manager.create(scale="xs", workers=1, name="runtime-xs")
    runtime.state = "deployed"
    runtime._write_metadata()
    registry = ManagedBucketRegistry(manager.telemetry_ownership_file)
    registry.record_created(runtime.workers[0].bucket, runtime.id)
    calls = []
    _record_lifecycle_operations(monkeypatch, calls, fail_stage="teardown")

    with pytest.raises(Exception, match="teardown"):
        runtime.teardown()

    entry = json.loads(manager.telemetry_ownership_file.read_text())["buckets"][runtime.workers[0].bucket]
    assert entry["retired_at"] is None
    assert runtime.root_dir.exists()


def test_created_runtime_teardown_is_metadata_only(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod
    from netopsbench.platform.runtime import lifecycle

    runtime = runtimes_mod.RuntimeManager(workspace=tmp_path).create(scale="xs", workers=1, name="runtime-xs")
    monkeypatch.setattr(
        lifecycle,
        "teardown_workers",
        lambda *_args, **_kwargs: pytest.fail("created runtime has no physical workers"),
    )

    runtime.teardown()

    assert runtime.state == "torn_down"
    assert not runtime.root_dir.exists()


def test_teardown_workers_attempts_all_workers_and_aggregates_failures(monkeypatch, tmp_path):
    from netopsbench.models.runtime import RuntimeIdentity
    from netopsbench.platform.runtime import lifecycle

    workers = [
        RuntimeIdentity.create(
            runtime_id="runtime-xs",
            worker_id=f"worker-{index}",
            worker_index=index,
            lab_name=f"lab-{index}",
            topology_dir=tmp_path / f"worker-{index}",
            mgmt_subnet=f"172.31.{index}.0/24",
            mgmt_network=f"network-{index}",
        )
        for index in range(1, 4)
    ]
    attempted = []

    def fake_teardown(worker, _registry):
        attempted.append(worker.lab_name)
        if worker.worker_index != 2:
            raise RuntimeError(f"failure-{worker.worker_index}")

    monkeypatch.setattr(lifecycle, "teardown_worker_lab", fake_teardown)

    with pytest.raises(RuntimeError, match=r"lab-1.*failure-1.*lab-3.*failure-3"):
        lifecycle.teardown_workers(workers)

    assert attempted == ["lab-1", "lab-2", "lab-3"]


def test_worker_teardown_without_generated_manifest_uses_exact_lab_name(monkeypatch, tmp_path):
    from netopsbench.models.runtime import RuntimeIdentity
    from netopsbench.platform.runtime import deployment

    topology_dir = tmp_path / "worker-1"
    topology_dir.mkdir()
    worker = RuntimeIdentity.create(
        runtime_id="runtime-xs",
        worker_id="worker-1",
        worker_index=1,
        lab_name="runtime-xs",
        topology_dir=topology_dir,
        mgmt_subnet="172.31.1.0/24",
        mgmt_network="clab-mgmt-runtime-xs",
    )
    commands = []
    monkeypatch.setattr(deployment, "safe_run", lambda command, **_kwargs: commands.append(command))
    monkeypatch.setattr(deployment, "_stop_collector", lambda _path: None)
    monkeypatch.setattr(deployment, "_lab_container_names", lambda *_args: [])
    monkeypatch.setattr(deployment, "_wait_for_lab_removal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "docker_prefix", lambda: [])
    monkeypatch.setattr(deployment, "sudo_prefix", lambda: [])

    deployment.teardown_worker_lab(worker)

    assert ["containerlab", "destroy", "--name", "runtime-xs", "--cleanup"] in commands


def test_worker_teardown_uses_scale_profile_timeout(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from netopsbench.models.runtime import RuntimeIdentity
    from netopsbench.platform.runtime import deployment

    topology_dir = tmp_path / "worker-1"
    topology_dir.mkdir()
    (topology_dir / "topology.json").write_text("{}", encoding="utf-8")
    (topology_dir / "runtime-k12.clab.yaml").write_text("name: runtime-k12\n", encoding="utf-8")
    worker = RuntimeIdentity.create(
        runtime_id="runtime-k12",
        worker_id="worker-1",
        worker_index=1,
        lab_name="runtime-k12",
        topology_dir=topology_dir,
        mgmt_subnet="172.31.1.0/24",
        mgmt_network="clab-mgmt-runtime-k12",
    )
    calls = []

    def fake_safe_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deployment, "safe_run", fake_safe_run)
    monkeypatch.setattr(deployment, "_stop_collector", lambda _path: None)
    monkeypatch.setattr(deployment, "_lab_container_names", lambda *_args: [])
    monkeypatch.setattr(deployment, "_wait_for_lab_removal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "docker_prefix", lambda: [])
    monkeypatch.setattr(deployment, "sudo_prefix", lambda: [])
    monkeypatch.setattr(
        deployment,
        "load_topology_manifest",
        lambda _path: SimpleNamespace(scale="fat-tree-k12"),
    )

    deployment.teardown_worker_lab(worker)

    destroy_call = next(call for call in calls if "containerlab" in call[0])
    assert destroy_call[1]["timeout"] == 5400


def test_provision_cleans_up_metadata_on_deploy_failure(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    calls = []
    _record_lifecycle_operations(monkeypatch, calls, fail_stage="deploy")
    monkeypatch.setattr(runtimes_mod, "teardown_workers", lambda *_args, **_kwargs: None)
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


def test_provision_preserves_quarantined_metadata_when_cleanup_fails(tmp_path, monkeypatch):
    import netopsbench.platform.runtime.manager as runtimes_mod

    calls = []
    _record_lifecycle_operations(monkeypatch, calls, fail_stage="deploy")

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(runtimes_mod, "teardown_workers", fail_cleanup)
    manager = runtimes_mod.RuntimeManager(workspace=tmp_path)
    runtime_dir = tmp_path / ".netopsbench" / "runtimes" / "will-quarantine"

    with pytest.raises(RuntimeError, match="provisioning failed.*cleanup failed"):
        manager.provision(scale="xs", workers=1, name="will-quarantine")

    assert runtime_dir.exists()
    runtime = manager.get("will-quarantine")
    assert runtime is not None
    assert runtime.state == "cleanup_failed"
    assert runtime.metadata["quarantined"] is True
    assert "cleanup failed" in str(runtime.metadata["cleanup_error"])
