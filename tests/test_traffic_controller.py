import subprocess
from types import SimpleNamespace

import pytest

from netopsbench.platform.traffic import controller as controller_mod
from netopsbench.platform.traffic import scenario_execution as scenario_execution_mod
from netopsbench.platform.traffic.controller import TrafficController, TrafficFlow


def _flows() -> list[TrafficFlow]:
    return [
        TrafficFlow(src="client1", dst="client3", dst_ip="192.168.103.2", dst_port=5201, protocol="udp"),
        TrafficFlow(src="client1", dst="client4", dst_ip="192.168.104.2", dst_port=5202, protocol="tcp"),
        TrafficFlow(src="client2", dst="client3", dst_ip="192.168.103.2", dst_port=5201, protocol="udp"),
        TrafficFlow(src="client2", dst="client4", dst_ip="192.168.104.2", dst_port=5202, protocol="tcp"),
    ]


def _controller() -> TrafficController:
    return TrafficController(
        {
            "client1": "clab-test-client1",
            "client2": "clab-test-client2",
            "client3": "clab-test-client3",
            "client4": "clab-test-client4",
        }
    )


def test_start_matrix_batches_server_ensure_and_client_start_by_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    command_texts = [" ".join(call) for call in calls]
    server_calls = [text for text in command_texts if "iperf3 -s" in text]
    client_calls = [text for text in command_texts if "iperf3 -c" in text]

    assert len(flow_ids) == 4
    assert len(controller.active_flows) == 4
    assert len(server_calls) == 2
    assert len(client_calls) == 2
    assert any("192.168.103.2" in text and "192.168.104.2" in text for text in client_calls)


def test_batched_server_ensure_fails_fast_and_verifies_listeners(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    controller._ensure_iperf_servers_batch("clab-test-client3", {5201, 5202})

    script = calls[0][-1]
    assert script.startswith("set -e\n")
    assert script.count("ss -lntH") == 2
    assert "required_ports='5201 5202'" in script
    assert "for port in $required_ports" in script
    assert "missing=0" in script


def test_batched_source_start_is_idempotent_for_safe_retry(monkeypatch):
    calls: list[list[str]] = []

    monkeypatch.setattr(
        controller_mod,
        "safe_run",
        lambda cmd, **kwargs: calls.append([str(part) for part in cmd]) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    _controller().start_matrix(_flows())

    source_scripts = [call[-1] for call in calls if "iperf3 -c" in " ".join(call)]
    assert source_scripts
    assert all("flow_running" in script for script in source_scripts)
    assert all("/tmp/netopsbench-traffic/" in script for script in source_scripts)
    assert all("/proc/$pid/cmdline" in script for script in source_scripts)
    assert all("</dev/null &" in script for script in source_scripts)
    assert all("missing=0" in script for script in source_scripts)
    assert all("pgrep -f" not in script for script in source_scripts)
    assert all('flow_running "$pid_file" || rm -f "$pid_file"' in script for script in source_scripts)
    assert all('if flow_running "$pid_file"; then kill' not in script for script in source_scripts)


def test_start_matrix_retries_transient_batch_failure_at_lower_parallelism(monkeypatch):
    attempts: dict[str, int] = {}

    def fake_safe_run(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        container = next(part for part in cmd if str(part).startswith("clab-test-client"))
        key = f"{container}:{'server' if 'iperf3 -s' in text else 'source'}"
        attempts[key] = attempts.get(key, 0) + 1
        if key == "clab-test-client3:server" and attempts[key] == 1:
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    assert len(flow_ids) == 4
    assert attempts["clab-test-client3:server"] == 2
    assert controller.last_start_stats.server_first_attempt_successes == 1
    assert controller.last_start_stats.server_first_attempt_failures == 1
    assert controller.last_start_stats.retry_count == 1
    assert controller.last_start_stats.timeout_count == 1
    assert controller.last_start_stats.started_flow_count == 4
    assert controller.last_start_stats.failed_flow_count == 0


def test_stop_all_cleans_clients_and_servers_in_every_client_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)

    controller = _controller()
    controller.start_matrix(_flows())
    calls.clear()

    controller.stop_all()

    command_texts = [" ".join(call) for call in calls]
    stop_calls = [text for text in command_texts if "/tmp/netopsbench-traffic" in text]
    assert len(stop_calls) == 4
    assert all("flow_running" in text for text in stop_calls)
    assert all("/proc/[0-9]*" in text for text in stop_calls)
    assert all('"iperf3 -c "*' in text for text in stop_calls)
    assert all('"iperf3 -s -D "' in text for text in stop_calls)
    assert all("iperf3 -s -D -p 5204" in text for text in stop_calls)
    assert controller.active_flows == {}


def test_stop_all_cleans_unrecorded_traffic(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        controller_mod,
        "safe_run",
        lambda cmd, **kwargs: calls.append([str(part) for part in cmd]) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    controller = _controller()
    controller.stop_all()

    assert len(calls) == 4
    assert all("iperf3 -s -D -p 5201" in call[-1] for call in calls)


def test_stop_all_attempts_every_container_and_reports_cleanup_failure(monkeypatch):
    calls: list[str] = []

    def fake_safe_run(cmd, **kwargs):
        container = next(str(part) for part in cmd if str(part).startswith("clab-test-client"))
        calls.append(container)
        if container == "clab-test-client2":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)
    controller = _controller()

    with pytest.raises(RuntimeError, match="clab-test-client2: exit status 1"):
        controller.stop_all()

    assert sorted(calls) == [f"clab-test-client{index}" for index in range(1, 5)]


def test_traffic_parallelism_env_override_and_invalid_value(monkeypatch):
    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "7")
    assert controller_mod._traffic_parallelism() == 7

    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "not-an-int")
    assert controller_mod._traffic_parallelism() == 32

    monkeypatch.delenv("NETOPSBENCH_TRAFFIC_PARALLELISM", raising=False)
    assert controller_mod._traffic_parallelism() == 32


@pytest.mark.parametrize(
    ("configured", "server", "retry"),
    [(32, 16, 4), (8, 4, 4), (1, 1, 1), (64, 16, 4)],
)
def test_controller_derives_server_and_retry_parallelism(configured, server, retry):
    controller = TrafficController({}, parallelism=configured)

    assert controller.parallelism == configured
    assert controller.server_parallelism == server
    assert controller.retry_parallelism == retry


def test_start_matrix_partial_failure_records_only_started_flows(monkeypatch):
    messages: list[str] = []

    def fake_safe_run(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        if "clab-test-client2" in text and "iperf3 -c" in text:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)
    monkeypatch.setattr(
        controller_mod.logger,
        "warning",
        lambda message, *args, **kwargs: messages.append(message % args if args else message),
    )
    monkeypatch.setenv("NETOPSBENCH_TRAFFIC_PARALLELISM", "2")

    controller = _controller()
    flow_ids = controller.start_matrix(_flows())

    assert len(flow_ids) == 2
    assert {flow.src for flow in controller.active_flows.values()} == {"client1"}
    source_failures = [message for message in messages if "Failed to start traffic source" in message]
    assert source_failures == ["Failed to start traffic source clab-test-client2 (2 flows): boom"]


def test_verify_active_flows_checks_client_pids_and_server_listeners(monkeypatch):
    calls: list[list[str]] = []

    def fake_safe_run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)
    controller = _controller()
    controller.start_matrix(_flows())
    calls.clear()

    assert controller.verify_active_flows() is True
    assert len(calls) == 4
    assert all("ss -lntH" in call[-1] or "flow_running" in call[-1] for call in calls)
    assert any("192.168.103.2" not in call[-1] and "5201" in call[-1] for call in calls)


def test_verify_active_flows_rejects_a_missing_client_group(monkeypatch):
    def fake_safe_run(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        if "clab-test-client2" in text and "flow_running" in text and "nohup" not in text:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(controller_mod, "safe_run", fake_safe_run)
    controller = _controller()
    controller.start_matrix(_flows())

    assert controller.verify_active_flows() is False


def test_setup_traffic_cleans_partial_matrix_and_fails_before_baseline(tmp_path, monkeypatch):
    (tmp_path / "topology.json").write_text("{}", encoding="utf-8")
    traffic_config = {
        "stats": {
            "total_flows": 2,
            "udp_flows": 1,
            "tcp_flows": 1,
            "estimated_switch_pps": {},
        },
        "profile": {},
        "flows": [
            {
                "src": "client1",
                "dst": "client2",
                "dst_ip": "192.0.2.2",
                "protocol": "udp",
            },
            {
                "src": "client2",
                "dst": "client1",
                "dst_ip": "192.0.2.1",
                "protocol": "tcp",
            },
        ],
    }
    controller_instances = []

    class PartialController:
        def __init__(self, _containers):
            self.last_start_stats = SimpleNamespace(to_dict=lambda: {"started_flow_count": 1})
            self.stop_calls = 0
            controller_instances.append(self)

        def start_matrix(self, flows):
            return [flows[0].flow_id]

        def stop_all(self):
            self.stop_calls += 1

    topology = {
        "name": "test",
        "devices": {"clients": [{"name": "client1"}, {"name": "client2"}]},
    }
    monkeypatch.setattr(scenario_execution_mod, "generate_traffic_config", lambda *args, **kwargs: traffic_config)
    monkeypatch.setattr(scenario_execution_mod, "validate_traffic_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scenario_execution_mod,
        "load_topology_manifest",
        lambda _path: SimpleNamespace(to_agent_topology=lambda: topology),
    )
    monkeypatch.setattr(scenario_execution_mod, "TrafficController", PartialController)
    runner = SimpleNamespace(topology_dir=tmp_path, scale_registry=SimpleNamespace(), traffic_controller=None)

    with pytest.raises(RuntimeError, match="started 1/2 flows"):
        scenario_execution_mod.setup_traffic(runner, "xs", "standard")

    assert controller_instances[0].stop_calls == 1
    assert runner.traffic_controller is None
