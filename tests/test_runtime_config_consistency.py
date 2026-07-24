"""Regression tests for streamlined runtime defaults and public examples."""

import subprocess
from pathlib import Path

import pytest

OBSERVABILITY_ASSETS = Path("netopsbench/platform/observability/assets")


def test_config_module_no_longer_exports_provider_registry_helpers():
    import netopsbench.config as config_mod

    assert not hasattr(config_mod, "LLM_PROVIDER_DEFAULTS")
    assert not hasattr(config_mod, "get_provider_defaults")
    assert not hasattr(config_mod, "get_openai_base_url")
    assert not hasattr(config_mod.NetOpsBenchConfig, "openai_base_url")


def test_update_telegraf_config_uses_packaged_template_and_central_defaults(tmp_path):
    import netopsbench.platform.observability.telegraf as telegraf_mod
    from netopsbench.platform.topology.generator import generate_topology

    topology_dir = tmp_path / "topology"
    generate_topology("xs", str(topology_dir), name="demo-runtime")
    topo_path = topology_dir / "topology.json"

    output_path = tmp_path / "telegraf.conf"
    rc = telegraf_mod.update_telegraf_config(str(topo_path), output_file=str(output_path))

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert '"172.20.20.11:50051"' in rendered
    assert '"172.20.20.13:50051"' in rendered
    assert "http://influxdb:8086" in rendered
    assert "replace-me" in rendered
    assert "netopsbench" in rendered
    assert "demo-runtime" in rendered
    assert rendered.count('"172.20.20.11": "spine1"') == 1
    assert rendered.count('"172.20.20.13": "leaf1"') == 1
    assert 'pattern = "^172.20.20.11$"' not in rendered
    assert (
        'mapping_keys = ["source", "agent_host", "agent_ip", "agent", ' '"agent_address", "address", "target"]'
    ) in rendered
    assert "metric.tags[key] = ip_to_device[metric.tags[key]]" in rendered


def test_update_telegraf_config_rejects_legacy_grouped_topology(tmp_path):
    import json

    import pytest

    import netopsbench.platform.observability.telegraf as telegraf_mod

    topo_path = tmp_path / "topology.json"
    topo_path.write_text(
        json.dumps(
            {
                "name": "legacy",
                "devices": {"spines": [{"name": "spine1", "mgmt_ip": "172.20.20.11"}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version.*3.*Regenerate"):
        telegraf_mod.update_telegraf_config(str(topo_path), output_file=str(tmp_path / "telegraf.conf"))


def test_update_telegraf_config_requires_generated_configdb_artifacts(tmp_path):
    import netopsbench.platform.observability.telegraf as telegraf_mod
    from netopsbench.platform.topology.generator import generate_topology

    topology_dir = tmp_path / "topology"
    generate_topology("xs", str(topology_dir))
    (topology_dir / "configs" / "sonic" / "spine1" / "config_db.json").unlink()

    with pytest.raises(FileNotFoundError, match="Generated ConfigDB artifact"):
        telegraf_mod.update_telegraf_config(
            str(topology_dir / "topology.json"),
            output_file=str(tmp_path / "telegraf.conf"),
        )


def test_update_telegraf_config_isolates_gnmi_subscriptions_per_role(tmp_path):
    import netopsbench.platform.observability.telegraf as telegraf_mod
    from netopsbench.platform.topology.generator import generate_topology

    topology_dir = tmp_path / "topology"
    generate_topology("xlarge", str(topology_dir))
    topology_file = topology_dir / "topology.json"
    output_path = tmp_path / "telegraf.conf"
    rc = telegraf_mod.update_telegraf_config(str(topology_file), output_file=str(output_path))

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert rendered.count("[[inputs.gnmi]]") == 2
    assert "# gNMI role: spine" in rendered
    assert "# gNMI role: leaf" in rendered

    spine_block = rendered.split("# gNMI role: spine", 1)[1].split("# gNMI role:", 1)[0]
    leaf_block = rendered.split("# gNMI role: leaf", 1)[1].split("[[processors.regex]]", 1)[0]

    assert '"172.20.20.11:50051"' in spine_block
    assert '"172.20.20.27:50051"' not in spine_block
    assert 'path = "COUNTERS/Ethernet508"' in spine_block

    assert '"172.20.20.27:50051"' in leaf_block
    assert '"172.20.20.11:50051"' not in leaf_block
    assert 'path = "COUNTERS/Ethernet64"' in leaf_block
    assert 'path = "COUNTERS/Ethernet68"' not in leaf_block
    assert 'subscription_mode = "sample"' in rendered
    assert 'sample_interval = "10s"' in rendered
    assert 'username = "admin"' in rendered
    assert 'password = ""' in rendered
    assert 'encoding = "json_ietf"' in rendered
    assert 'target = "COUNTERS_DB"' in rendered
    assert rendered.count("fieldpass = [") == 2
    assert '"SAI_PORT_STAT_IF_IN_OCTETS"' in rendered
    assert '"SAI_PORT_STAT_IF_OUT_ERRORS"' in rendered
    assert "SAI_PORT_STAT_IF_IN_DROPPED_PKTS" not in rendered


def test_update_telegraf_config_scopes_native_fat_tree_roles_from_artifacts(tmp_path):
    import netopsbench.platform.observability.telegraf as telegraf_mod
    from netopsbench.platform.topology.generator import generate_topology

    topology_dir = tmp_path / "topology"
    generate_topology("fat-tree-k12", str(topology_dir), name="ft")

    output_path = tmp_path / "telegraf.conf"

    rc = telegraf_mod.update_telegraf_config(str(topology_dir / "topology.json"), output_file=str(output_path))

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert rendered.count("[[inputs.gnmi]]") == 3
    assert "# gNMI role: core" in rendered
    assert "# gNMI role: agg" in rendered
    assert "# gNMI role: edge" in rendered
    core_block = rendered.split("# gNMI role: core", 1)[1].split("# gNMI role:", 1)[0]
    agg_block = rendered.split("# gNMI role: agg", 1)[1].split("# gNMI role:", 1)[0]
    edge_block = rendered.split("# gNMI role: edge", 1)[1].split("[[processors.regex]]", 1)[0]
    assert '"172.20.20.11:50051"' in core_block
    assert '"172.20.20.47:50051"' not in core_block
    assert 'path = "COUNTERS/Ethernet44"' in core_block
    assert '"172.20.20.47:50051"' in agg_block
    assert 'path = "COUNTERS/Ethernet44"' in agg_block
    assert '"172.20.20.119:50051"' in edge_block
    assert 'path = "COUNTERS/Ethernet28"' in edge_block
    assert 'path = "COUNTERS/Ethernet32"' not in edge_block


def test_grafana_configs_use_runtime_scoping_variables():
    datasource_text = (OBSERVABILITY_ASSETS / "grafana/provisioning/datasources/default.yaml").read_text(
        encoding="utf-8"
    )
    compose_text = (OBSERVABILITY_ASSETS / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "${NETOPSBENCH_INFLUXDB_TOKEN}" in datasource_text
    assert "${NETOPSBENCH_INFLUXDB_BUCKET}" in datasource_text
    assert "${NETOPSBENCH_INFLUXDB_URL}" in datasource_text
    assert "NETOPSBENCH_INFLUXDB_TOKEN=${NETOPSBENCH_INFLUXDB_TOKEN:-replace-me}" in compose_text
    assert "NETOPSBENCH_INFLUXDB_BUCKET=${NETOPSBENCH_INFLUXDB_BUCKET:-netopsbench}" in compose_text
    assert "NETOPSBENCH_INFLUXDB_URL=http://influxdb:8086" in compose_text
    assert "image: influxdb@sha256:" in compose_text
    assert "image: grafana/grafana@sha256:" in compose_text
    assert "image: influxdb:" not in compose_text
    assert "image: grafana/grafana:" not in compose_text


def test_grafana_dashboards_parameterize_bucket_and_topology():
    dashboards = [
        OBSERVABILITY_ASSETS / "grafana/dashboards/network_overview.json",
        OBSERVABILITY_ASSETS / "grafana/dashboards/pingmesh.json",
    ]

    for dashboard_path in dashboards:
        text = dashboard_path.read_text(encoding="utf-8")
        assert "${bucket}" in text
        assert "${topology_id}" in text
        assert 'r.topology_id == \\"${topology_id}\\" or \\"${topology_id}\\" == \\"all\\"' in text
        assert '"name": "bucket"' in text
        assert '"name": "topology_id"' in text
        assert 'from(bucket: \\"netopsbench\\")' not in text


def test_network_overview_casts_interface_counters_before_derivative():
    text = (OBSERVABILITY_ASSETS / "grafana/dashboards/network_overview.json").read_text(encoding="utf-8")

    # Telegraf's gNMI interface counters arrive as string columns in Flux frames.
    # The dashboard must cast them before applying derivative(), or Grafana shows "No data".
    assert text.count("|> toFloat()\\n  |> derivative(unit: 1s, nonNegative: true)") >= 4


def test_packaged_observability_assets_enable_bgp_tail_input():
    dashboard_text = (OBSERVABILITY_ASSETS / "grafana/dashboards/network_overview.json").read_text(encoding="utf-8")
    telegraf_text = (OBSERVABILITY_ASSETS / "telegraf.conf.template").read_text(encoding="utf-8")

    assert "CPU Usage (optional)" not in dashboard_text
    assert "Memory Utilization (optional)" not in dashboard_text
    assert "/var/lib/netopsbench/bgp_neighbors*.lp" in telegraf_text
    assert "from_beginning = true" in telegraf_text
    assert 'watch_method = "poll"' in telegraf_text
    assert "metric_batch_size = 5000" in telegraf_text
    assert "metric_buffer_limit = 200000" in telegraf_text
    assert "[[inputs.influxdb_v2_listener]]" in telegraf_text
    assert 'service_address = ":8186"' in telegraf_text
    assert "max_undelivered_metrics = 200000" in telegraf_text
    assert 'parser_type = "upstream"' in telegraf_text
    assert 'content_encoding = "gzip"' in telegraf_text
    assert "/var/log/pingmesh/metrics.log" not in telegraf_text
    assert "debug = false" in telegraf_text
    assert "[[processors.printer]]" not in telegraf_text
    assert dashboard_text.count('group(columns: [\\"source\\", \\"neighbor_address\\"])\\n  |> last()') >= 3


@pytest.mark.parametrize(
    ("script", "args", "expected"),
    [
        (
            "scripts/runtime/deploy.sh",
            ["xlarge", "runtime-root"],
            [
                "-m",
                "netopsbench.platform.runtime.cli",
                "deploy",
                "xlarge",
                "runtime-root/generated_topology_xlarge",
                "test-lab",
                "172.31.180.0/23",
                "test-bucket",
                "--mgmt-network",
                "test-network",
            ],
        ),
        (
            "scripts/runtime/deploy_worker.sh",
            ["xs", "/tmp/topology", "test-lab", "172.31.101.0/24", "test-bucket"],
            [
                "-m",
                "netopsbench.platform.runtime.cli",
                "deploy",
                "xs",
                "/tmp/topology",
                "test-lab",
                "172.31.101.0/24",
                "test-bucket",
            ],
        ),
    ],
)
def test_runtime_shell_scripts_delegate_to_python_lifecycle(tmp_path, script, args, expected):
    capture = tmp_path / "args.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n', encoding="utf-8")
    fake_python.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "NETOPSBENCH_PYTHON": str(fake_python),
        "NETOPSBENCH_LAB_NAME": "test-lab",
        "NETOPSBENCH_MGMT_SUBNET": "172.31.180.0/23",
        "NETOPSBENCH_MGMT_NETWORK": "test-network",
        "NETOPSBENCH_INFLUXDB_BUCKET": "test-bucket",
        "CAPTURE_FILE": str(capture),
    }

    result = subprocess.run(["bash", script, *args], text=True, capture_output=True, env=env, check=False)

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").splitlines() == expected


def test_bgp_collector_size_default_is_owned_by_collector():
    from netopsbench.platform.observability.bgp_collector import (
        DEFAULT_BGP_COLLECTOR_MAX_BYTES,
        DEFAULT_BGP_COLLECTOR_PARALLELISM,
        DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS,
        DEFAULT_BGP_POLL_INTERVAL_SECONDS,
        DEFAULT_BGP_SPARSE_DEVICE_THRESHOLD,
    )

    assert DEFAULT_BGP_COLLECTOR_MAX_BYTES == 128 * 1024 * 1024
    assert DEFAULT_BGP_COLLECTOR_PARALLELISM == 16
    assert DEFAULT_BGP_FULL_SNAPSHOT_INTERVAL_SECONDS == 60
    assert DEFAULT_BGP_POLL_INTERVAL_SECONDS == 10
    assert DEFAULT_BGP_SPARSE_DEVICE_THRESHOLD == 128


def test_runtime_scripts_default_sonic_apply_parallelism_to_32():
    from netopsbench.platform.runtime.deployment import APPLY_CONFIG_PARALLELISM

    assert APPLY_CONFIG_PARALLELISM == 32


def test_env_example_does_not_expose_internal_runtime_parallelism():
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert "NETOPSBENCH_TRAFFIC_PARALLELISM" not in env_example
    assert "NETOPSBENCH_BGP_COLLECTOR_PARALLELISM" not in env_example
    assert "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES" not in env_example


def test_native_client_image_has_no_python_or_iperf_runtime():
    from netopsbench.platform.topology.config import (
        DEFAULT_CLIENT_IMAGE,
        DEFAULT_SONIC_VS_IMAGE,
    )

    dockerfile = Path("containers/client/Dockerfile").read_text(encoding="utf-8")

    runtime_stage = dockerfile.split("FROM alpine:3.24.1@", 1)[1]
    assert "python" not in runtime_stage.lower()
    assert "iperf" not in runtime_stage.lower()
    assert "/usr/local/bin/netopsbench-client-agent" in runtime_stage
    assert DEFAULT_CLIENT_IMAGE.startswith("docker.io/yyyyyt123/netopsbench-client@sha256:")
    assert ":latest" not in DEFAULT_CLIENT_IMAGE
    assert DEFAULT_SONIC_VS_IMAGE.startswith("yyyyyt123/netopsbench-sonic-vs-202505-telemetry@sha256:")


def test_native_management_contract_matches_rust_constants():
    from netopsbench.platform.client_agent.contract import (
        CONTROL_PROTOCOL_VERSION,
        PINGMESH_CONTROL_PORT,
        TRAFFIC_CONTROL_PORT,
    )

    root = Path(__file__).resolve().parents[1]
    rust_config = (root / "native" / "client-agent" / "src" / "config.rs").read_text(encoding="utf-8")
    assert f"CONTROL_PROTOCOL_VERSION: u32 = {CONTROL_PROTOCOL_VERSION};" in rust_config
    assert f"PINGMESH_CONTROL_PORT: u16 = {PINGMESH_CONTROL_PORT};" in rust_config
    assert f"TRAFFIC_CONTROL_PORT: u16 = {TRAFFIC_CONTROL_PORT};" in rust_config
