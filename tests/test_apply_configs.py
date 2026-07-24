import subprocess

import pytest

from netopsbench.platform.runtime import apply_configs


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_wait_for_sonic_requires_configdb_after_vtysh(monkeypatch):
    calls = []

    def fake_safe_run(cmd, **kwargs):
        calls.append(cmd)
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, stdout="start.sh EXITED Jun 16 06:32 AM\n")
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(
                cmd,
                stdout=("redis-server RUNNING pid 10\n" "orchagent RUNNING pid 11\n" "zebra RUNNING pid 12\n"),
            )
        if "sonic-cfggen" in cmd:
            return _completed(cmd, returncode=0)
        if "vtysh" in cmd:
            return _completed(cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._wait_for_sonic("spine1", "clab-demo-spine1", max_tries=1, delay=0)
    assert any("sonic-cfggen" in cmd for cmd in calls)
    assert any("vtysh" in cmd for cmd in calls)


def test_wait_for_sonic_ignores_fatal_startup_when_services_are_ready(monkeypatch):
    calls = []

    def fake_safe_run(cmd, **kwargs):
        calls.append(cmd)
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, returncode=3, stdout="start.sh FATAL Exited too quickly\n")
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(
                cmd,
                stdout=(
                    "redis-server RUNNING pid 10\n"
                    "orchagent RUNNING pid 11\n"
                    "zebra RUNNING pid 12\n"
                    "bgpd RUNNING pid 13\n"
                ),
            )
        if "sonic-cfggen" in cmd:
            return _completed(cmd, returncode=0)
        if "vtysh" in cmd:
            return _completed(cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._wait_for_sonic("spine1", "clab-demo-spine1", max_tries=1, delay=0)
    repair_calls = [cmd for cmd in calls if "bash" in cmd and any("start.sh" in part for part in cmd)]
    assert repair_calls == []


def test_wait_for_sonic_does_not_repair_fatal_startup_without_configdb(monkeypatch):
    calls = []

    def fake_safe_run(cmd, **kwargs):
        calls.append(cmd)
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, returncode=3, stdout="start.sh FATAL Exited too quickly\n")
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(cmd, stdout="")
        if "sonic-cfggen" in cmd:
            return _completed(cmd, returncode=1, stderr="redis unavailable")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert not apply_configs._wait_for_sonic("spine12", "clab-demo-spine12", max_tries=2, delay=0)
    repair_calls = [cmd for cmd in calls if "bash" in cmd and any("start.sh" in part for part in cmd)]
    assert repair_calls == []


def test_wait_for_sonic_requires_key_supervisor_services(monkeypatch):
    def fake_safe_run(cmd, **kwargs):
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, stdout="start.sh FATAL Exited too quickly\n")
        if "sonic-cfggen" in cmd:
            return _completed(cmd, returncode=0)
        if "vtysh" in cmd:
            return _completed(cmd, returncode=0)
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(
                cmd,
                returncode=3,
                stdout=(
                    "redis-server RUNNING pid 10\n"
                    "orchagent STOPPED Not started\n"
                    "zebra RUNNING pid 12\n"
                    "bgpd RUNNING pid 13\n"
                ),
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert not apply_configs._wait_for_sonic("spine1", "clab-demo-spine1", max_tries=1, delay=0)


def test_wait_for_sonic_requires_complete_counters_port_map(monkeypatch):
    def fake_safe_run(cmd, **kwargs):
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, stdout="start.sh EXITED Jun 16 06:32 AM\n")
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(
                cmd,
                stdout=("redis-server RUNNING pid 10\n" "orchagent RUNNING pid 11\n" "zebra RUNNING pid 12\n"),
            )
        if "sonic-cfggen" in cmd or "vtysh" in cmd:
            return _completed(cmd, returncode=0)
        if "redis-cli" in cmd:
            return _completed(cmd, stdout="7\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert not apply_configs._wait_for_sonic(
        "edge1",
        "clab-demo-edge1",
        max_tries=1,
        delay=0,
        expected_port_count=8,
    )


def test_wait_for_sonic_accepts_complete_counters_port_map(monkeypatch):
    def fake_safe_run(cmd, **kwargs):
        if "supervisorctl" in cmd and "status" in cmd and "start.sh" in cmd:
            return _completed(cmd, stdout="start.sh EXITED Jun 16 06:32 AM\n")
        if "supervisorctl" in cmd and "status" in cmd:
            return _completed(
                cmd,
                stdout=("redis-server RUNNING pid 10\n" "orchagent RUNNING pid 11\n" "zebra RUNNING pid 12\n"),
            )
        if "sonic-cfggen" in cmd or "vtysh" in cmd:
            return _completed(cmd, returncode=0)
        if "redis-cli" in cmd:
            return _completed(cmd, stdout="8\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._wait_for_sonic(
        "edge1",
        "clab-demo-edge1",
        max_tries=1,
        delay=0,
        expected_port_count=8,
    )


def test_discover_devices_uses_preseed_layout_only(tmp_path):
    sonic_dir = tmp_path / "configs" / "sonic"
    (sonic_dir / "spine1").mkdir(parents=True)
    (sonic_dir / "leaf1").mkdir(parents=True)
    (sonic_dir / "spine1" / "config_db.json").write_text("{}\n", encoding="utf-8")
    (sonic_dir / "leaf1" / "config_db.json").write_text("{}\n", encoding="utf-8")
    stale_dir = tmp_path / "configs"
    (stale_dir / "spine9.sh").write_text("# stale generated file from an old run\n", encoding="utf-8")

    assert apply_configs._discover_devices(str(tmp_path)) == ["spine1", "leaf1"]


def test_apply_single_device_preseed_activates_without_shell_copy(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs" / "sonic" / "spine1"
    config_dir.mkdir(parents=True)
    (config_dir / "config_db.json").write_text('{"PORT": {"Ethernet0": {}}}\n', encoding="utf-8")
    (config_dir / "port_config.ini").write_text("# ports\n", encoding="utf-8")
    (tmp_path / "configs" / "sonic" / "start.sh").write_text("# wrapper\n", encoding="utf-8")
    calls = []

    def fake_safe_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(cmd, returncode=0)

    monkeypatch.setattr(apply_configs, "docker_prefix", lambda: [])
    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)
    monkeypatch.setattr(apply_configs, "_wait_for_sonic", lambda *args, **kwargs: True)

    device, success, message, _elapsed, _ready_elapsed, _activation_elapsed = apply_configs._apply_single_device(
        "spine1", str(tmp_path), "demo", 1
    )

    assert (device, success, message) == ("spine1", True, "post-deploy activation")
    assert not any(cmd[:2] == ["docker", "cp"] for cmd in calls)
    assert not any(cmd[-2:] == ["bash", "/tmp/config.sh"] for cmd in calls)
    joined = "\n".join(" ".join(cmd) for cmd in calls)
    assert "fib_multipath_hash_policy=1" in joined
    assert "sysctl -n net.ipv4.fib_multipath_hash_policy" in joined
    assert "fib_multipath_hash_policy=1 >/dev/null || true" not in joined
    assert "vtysh -b" in joined
    assert "counterpoll port interval 10000" in joined
    assert "/usr/sbin/telemetry -port 50051 -noTLS -client_auth none" in joined
    assert "pkill -x telemetry" in joined


def test_expected_interface_addresses_ignores_marker_keys(tmp_path):
    config = tmp_path / "config_db.json"
    config.write_text(
        (
            '{"INTERFACE": {'
            '"Ethernet0": {}, '
            '"Ethernet0|10.1.1.2/30": {}, '
            '"Ethernet4|10.2.1.2/30": {}'
            "}}\n"
        ),
        encoding="utf-8",
    )

    assert apply_configs._expected_interface_addresses(str(config)) == {
        "Ethernet0": {"10.1.1.2/30"},
        "Ethernet4": {"10.2.1.2/30"},
    }


def test_reconcile_preseed_interfaces_repairs_only_missing_kernel_address(monkeypatch, tmp_path):
    config = tmp_path / "config_db.json"
    config.write_text(
        (
            '{"INTERFACE": {'
            '"Ethernet0|10.1.1.2/30": {}, '
            '"Ethernet4|10.2.1.2/30": {}'
            "}}\n"
        ),
        encoding="utf-8",
    )
    calls = []
    inspection_count = 0

    def fake_safe_run(cmd, **kwargs):
        nonlocal inspection_count
        calls.append(cmd)
        if cmd[-4:] == ["ip", "-j", "address", "show"]:
            inspection_count += 1
            addresses = [
                {
                    "ifname": "Ethernet4",
                    "addr_info": [{"local": "10.2.1.2", "prefixlen": 30}],
                }
            ]
            if inspection_count > 1:
                addresses.append(
                    {
                        "ifname": "Ethernet0",
                        "addr_info": [{"local": "10.1.1.2", "prefixlen": 30}],
                    }
                )
            return _completed(cmd, stdout=__import__("json").dumps(addresses))
        return _completed(cmd)

    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._reconcile_preseed_interfaces([], "clab-demo-leaf1", str(config)) == []
    assert any(cmd[-6:] == ["ip", "link", "set", "dev", "Ethernet0", "up"] for cmd in calls)
    assert any(
        cmd[-6:] == ["ip", "address", "replace", "10.1.1.2/30", "dev", "Ethernet0"]
        for cmd in calls
    )
    assert not any("Ethernet4" in cmd and "replace" in cmd for cmd in calls)


def test_reconcile_preseed_interfaces_reports_persistent_drift(monkeypatch, tmp_path):
    config = tmp_path / "config_db.json"
    config.write_text('{"INTERFACE": {"Ethernet0|10.1.1.2/30": {}}}\n', encoding="utf-8")

    def fake_safe_run(cmd, **kwargs):
        if cmd[-4:] == ["ip", "-j", "address", "show"]:
            return _completed(cmd, stdout="[]")
        return _completed(cmd)

    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._reconcile_preseed_interfaces([], "clab-demo-leaf1", str(config)) == [
        "Ethernet0|10.1.1.2/30"
    ]


def test_apply_single_device_requires_preseed_config(monkeypatch, tmp_path):
    wait_called = False

    def fake_wait(*_args, **_kwargs):
        nonlocal wait_called
        wait_called = True
        return True

    monkeypatch.setattr(apply_configs, "_wait_for_sonic", fake_wait)

    device, success, message, _elapsed, _ready_elapsed, _activation_elapsed = apply_configs._apply_single_device(
        "spine1", str(tmp_path), "demo", 1
    )

    assert (device, success) == ("spine1", False)
    assert "preseed config not found" in message
    assert wait_called is False


def test_apply_single_device_requires_startup_wrapper(monkeypatch, tmp_path):
    config_dir = tmp_path / "configs" / "sonic" / "spine1"
    config_dir.mkdir(parents=True)
    (config_dir / "config_db.json").write_text("{}\n", encoding="utf-8")
    wait_called = False

    def fake_wait(*_args, **_kwargs):
        nonlocal wait_called
        wait_called = True
        return True

    monkeypatch.setattr(apply_configs, "_wait_for_sonic", fake_wait)

    device, success, message, _elapsed, _ready_elapsed, _activation_elapsed = apply_configs._apply_single_device(
        "spine1", str(tmp_path), "demo", 1
    )

    assert (device, success) == ("spine1", False)
    assert "startup wrapper not found" in message
    assert wait_called is False


def test_activate_preseed_device_fails_when_hash_policy_cannot_be_applied(monkeypatch):
    calls = []

    def fake_safe_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(cmd, returncode=1, stderr="sysctl: permission denied")

    monkeypatch.setattr(apply_configs, "safe_run", fake_safe_run)

    assert apply_configs._activate_preseed_device([], "clab-demo-agg1", 0) is False
    command = calls[0][-1]
    assert "fib_multipath_hash_policy=0" in command
    assert "sysctl -n net.ipv4.fib_multipath_hash_policy" in command


def test_ecmp_hash_policies_are_loaded_from_manifest_roles(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    generate_topology("fat-tree-k12", str(tmp_path))
    manifest = load_topology_manifest(tmp_path)

    policies = apply_configs._ecmp_hash_policies(manifest, ["core1", "agg1", "edge1"])

    assert policies == {"core1": 1, "agg1": 0, "edge1": 1}


def test_ecmp_hash_policies_reject_preseed_device_missing_from_manifest(tmp_path):
    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    generate_topology("xs", str(tmp_path))
    manifest = load_topology_manifest(tmp_path)

    with pytest.raises(RuntimeError, match="stale1"):
        apply_configs._ecmp_hash_policies(manifest, ["spine1", "stale1"])
