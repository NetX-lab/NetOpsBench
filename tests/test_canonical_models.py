"""Focused contracts for the architecture refactor's canonical models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from netopsbench.models import topology as topology_models
from netopsbench.models.profiles import get_scale_profile, supported_scales
from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.models.topology import (
    Collector,
    Device,
    DeviceRole,
    Link,
    LinkEndpoint,
    Management,
    TopologyManifest,
)


def _manifest(*, devices: list[Device], links: list[Link]) -> TopologyManifest:
    return TopologyManifest(
        topology_id="demo-topology",
        name="demo",
        scale="xs",
        family="clos",
        management=Management(network="clab-demo", ipv4_subnet="172.20.20.0/24"),
        collector=Collector(ipv4="172.20.20.200"),
        defaults=topology_models.TopologyDefaults(),
        facts=topology_models.TopologyFacts(
            num_spines=2,
            num_leafs=1,
            clients_per_attached_switch=1,
            total_clients=sum(device.role is DeviceRole.CLIENT for device in devices),
            total_switches=sum(device.role is not DeviceRole.CLIENT for device in devices),
        ),
        routing=topology_models.RoutingMetadata(
            ecmp_hash_policy_by_role={device.role: 1 for device in devices if device.role is not DeviceRole.CLIENT}
        ),
        devices=devices,
        links=links,
    )


def test_topology_manifest_validates_device_and_link_references():
    spine = Device(name="spine1", role=DeviceRole.SPINE)
    leaf = Device(name="leaf1", role=DeviceRole.LEAF)
    client = Device(name="client1", role=DeviceRole.CLIENT, attached_switch="leaf1")
    link = Link(
        kind="spine-leaf",
        endpoints=(
            LinkEndpoint(device="spine1", interface="Ethernet0"),
            LinkEndpoint(device="leaf1", interface="Ethernet0"),
        ),
    )

    manifest = _manifest(devices=[spine, leaf, client], links=[link])

    assert manifest.device("leaf1") == leaf
    assert manifest.devices_by_role("spine") == [spine]
    assert manifest.switches() == [spine, leaf]
    assert manifest.routing_devices() == [spine, leaf]
    assert manifest.edge_devices() == [leaf]
    assert manifest.clients() == [client]
    assert manifest.client_attached_devices() == [leaf]

    with pytest.raises(ValidationError, match="unique"):
        _manifest(devices=[spine, Device(name="spine1", role=DeviceRole.LEAF)], links=[])

    with pytest.raises(ValidationError, match="unknown device"):
        _manifest(
            devices=[spine],
            links=[
                Link(
                    kind="invalid",
                    endpoints=(
                        LinkEndpoint(device="spine1", interface="Ethernet0"),
                        LinkEndpoint(device="missing", interface="Ethernet0"),
                    ),
                )
            ],
        )


def test_topology_manifest_requires_clients_to_attach_to_known_non_clients():
    leaf = Device(name="leaf1", role=DeviceRole.LEAF)

    with pytest.raises(ValidationError, match="must define attached_switch"):
        _manifest(devices=[leaf, Device(name="client1", role=DeviceRole.CLIENT)], links=[])

    with pytest.raises(ValidationError, match="unknown device"):
        _manifest(
            devices=[leaf, Device(name="client1", role=DeviceRole.CLIENT, attached_switch="missing")],
            links=[],
        )


@pytest.mark.parametrize("field", ["mgmt_ip", "data_ip", "router_id"])
def test_topology_manifest_rejects_duplicate_device_addresses(field):
    devices = [
        Device(name="leaf1", role=DeviceRole.LEAF, **{field: "192.0.2.1"}),
        Device(name="leaf2", role=DeviceRole.LEAF, **{field: "192.0.2.1"}),
    ]
    with pytest.raises(ValidationError, match=rf"{field} values must be unique"):
        _manifest(devices=devices, links=[])


def test_topology_manifest_requires_exact_ecmp_policy_for_switch_roles():
    spine = Device(name="spine1", role=DeviceRole.SPINE)
    leaf = Device(name="leaf1", role=DeviceRole.LEAF)
    payload = _manifest(devices=[spine, leaf], links=[]).model_dump(mode="json")

    del payload["routing"]["ecmp_hash_policy_by_role"]["leaf"]
    with pytest.raises(ValidationError, match="missing roles: leaf"):
        TopologyManifest.model_validate(payload)

    payload = _manifest(devices=[spine], links=[]).model_dump(mode="json")
    payload["routing"]["ecmp_hash_policy_by_role"]["leaf"] = 1
    with pytest.raises(ValidationError, match="unexpected roles: leaf"):
        TopologyManifest.model_validate(payload)

    payload = _manifest(devices=[spine], links=[]).model_dump(mode="json")
    payload["routing"]["ecmp_hash_policy_by_role"]["spine"] = 2
    with pytest.raises(ValidationError, match="literal_error"):
        TopologyManifest.model_validate(payload)

    with pytest.raises(ValidationError, match="non-client"):
        _manifest(
            devices=[
                leaf,
                Device(name="client1", role=DeviceRole.CLIENT, attached_switch="leaf1"),
                Device(name="client2", role=DeviceRole.CLIENT, attached_switch="client1"),
            ],
            links=[],
        )


def test_agent_projection_preserves_canonical_fat_tree_roles():
    core = Device(name="core1", role="core", mgmt_ip="172.20.20.11", asn=65001)
    agg = Device(name="agg1", role="agg", mgmt_ip="172.20.20.12", asn=65101)
    edge = Device(name="edge1", role="edge", mgmt_ip="172.20.20.13", asn=65201)
    client = Device(name="client1", role="client", attached_switch="edge1", data_ip="192.168.1.2")
    manifest = TopologyManifest(
        topology_id="fat-demo",
        name="fat-demo",
        scale="fat-tree-k8",
        family="fat-tree",
        management=Management(network="clab-fat-demo", ipv4_subnet="172.20.20.0/24"),
        collector=Collector(ipv4="172.20.20.200"),
        defaults=topology_models.TopologyDefaults(),
        facts=topology_models.TopologyFacts(
            num_cores=1,
            num_aggs=1,
            num_edges=1,
            num_pods=1,
            clients_per_attached_switch=1,
            total_clients=1,
            total_switches=3,
            fat_tree_k=2,
            full_density_clients_per_attached_switch=1,
            host_density="standard",
        ),
        routing=topology_models.RoutingMetadata(
            core_asn_range="65001-65001",
            agg_asn_range="65101-65101",
            edge_asn_range="65201-65201",
            ecmp_hash_policy_by_role={DeviceRole.CORE: 1, DeviceRole.AGG: 0, DeviceRole.EDGE: 1},
        ),
        devices=[core, agg, edge, client],
        links=[],
    )

    projected = manifest.to_agent_topology()

    assert "spines" not in projected["devices"]
    assert "leafs" not in projected["devices"]
    assert [device["name"] for device in projected["devices"]["cores"]] == ["core1"]
    assert [device["name"] for device in projected["devices"]["aggs"]] == ["agg1"]
    assert [device["name"] for device in projected["devices"]["edges"]] == ["edge1"]
    projected_client = projected["devices"]["clients"][0]
    assert projected_client["name"] == "client1"
    assert projected_client["data_ip"] == "192.168.1.2"
    assert projected_client["attached_switch"] == "edge1"
    assert projected_client["edge"] == "edge1"
    assert "leaf" not in projected_client
    assert projected["defaults"] == {"link_mtu": 9232, "sonic_port_mtu": 9100}
    assert projected["fat_tree_k"] == 2
    assert projected["scale"] == {
        "name": "fat-tree-k8",
        "num_core": 1,
        "num_agg": 1,
        "num_edge": 1,
        "num_pods": 1,
        "clients_per_edge": 1,
        "full_density_clients_per_edge": 1,
        "host_density": "standard",
        "total_clients": 1,
        "total_devices": 3,
    }
    assert projected["routing"]["core_asn_range"] == "65001-65001"
    assert projected["routing"]["ecmp_hash_policy_by_role"] == {"core": 1, "agg": 0, "edge": 1}
    assert [device["role"] for device in manifest.model_dump(mode="json")["devices"]] == [
        "core",
        "agg",
        "edge",
        "client",
    ]


def test_agent_projection_metadata_cannot_overwrite_canonical_device_fields():
    spine = Device(
        name="spine1",
        role=DeviceRole.SPINE,
        mgmt_ip="172.20.20.11",
        asn=65001,
        metadata={
            "name": "metadata-name",
            "role": "client",
            "mgmt_ip": "192.0.2.1",
            "asn": 99999,
            "site": "lab-a",
        },
    )

    projected = _manifest(devices=[spine], links=[]).to_agent_topology()["devices"]["spines"][0]

    assert projected["name"] == "spine1"
    assert projected["mgmt_ip"] == "172.20.20.11"
    assert projected["asn"] == 65001
    assert "role" not in projected
    assert projected["site"] == "lab-a"


def test_topology_defaults_facts_and_links_are_typed_and_strict():
    defaults = topology_models.TopologyDefaults()
    facts = topology_models.TopologyFacts(
        num_spines=16,
        num_leafs=128,
        clients_per_attached_switch=1,
        total_clients=128,
        total_switches=144,
    )
    link = Link(
        kind="spine-leaf",
        mtu=9232,
        endpoints=(
            LinkEndpoint(device="spine1", interface="eth1"),
            LinkEndpoint(device="leaf1", interface="eth1"),
        ),
    )

    assert defaults.model_dump() == {"link_mtu": 9232, "sonic_port_mtu": 9100}
    assert facts.clients_per_attached_switch == 1
    assert facts.num_leafs == 128
    assert link.mtu == 9232

    with pytest.raises(ValidationError, match="extra"):
        topology_models.TopologyDefaults(link_mtu=9232, sonic_port_mtu=9100, legacy_mtu=1500)

    with pytest.raises(ValidationError, match="extra"):
        topology_models.TopologyFacts(
            clients_per_attached_switch=1,
            total_clients=2,
            total_switches=4,
            num_spines=2,
            num_leafs=2,
            legacy_count=4,
        )


def test_runtime_identity_defaults_are_isolated_by_runtime_id():
    first = RuntimeIdentity.create(
        runtime_id="Run A/1",
        worker_id="worker-1",
        worker_index=1,
        lab_name="lab-a",
        topology_dir=Path("/tmp/lab-a"),
        mgmt_subnet="172.31.100.0/24",
        mgmt_network="clab-mgmt-lab-a",
    )
    second = RuntimeIdentity.create(
        runtime_id="Run B/1",
        worker_id="worker-1",
        worker_index=1,
        lab_name="lab-b",
        topology_dir=Path("/tmp/lab-b"),
        mgmt_subnet="172.31.101.0/24",
        mgmt_network="clab-mgmt-lab-b",
    )

    assert first.topology_id == "lab-a"
    assert first.bucket == "network_data_run_a_1_w01"
    assert first.bucket != second.bucket
    assert not hasattr(first, "as_env")
    with pytest.raises(ValidationError):
        RuntimeIdentity.create(
            runtime_id="runtime",
            worker_id="worker",
            worker_index=0,
            lab_name="lab",
            topology_dir=Path("/tmp/lab"),
            mgmt_subnet="172.31.100.0/24",
            mgmt_network="clab-mgmt-lab",
        )


def test_runtime_identity_normal_constructor_applies_defaults():
    identity = RuntimeIdentity(
        runtime_id="Run Direct/1",
        worker_id="worker-2",
        worker_index=2,
        lab_name="lab-direct",
        topology_dir=Path("/tmp/lab-direct"),
        mgmt_subnet="172.31.102.0/24",
        mgmt_network="clab-mgmt-lab-direct",
    )

    assert identity.schema_version == "3"
    assert identity.topology_id == "lab-direct"
    assert identity.bucket == "network_data_run_direct_1_w02"


def test_runtime_identity_rejects_unknown_schema_version():
    with pytest.raises(ValidationError) as exc_info:
        RuntimeIdentity(
            schema_version="1",
            runtime_id="runtime",
            worker_id="worker-1",
            worker_index=1,
            lab_name="lab",
            topology_id="lab",
            topology_dir=Path("/tmp/lab"),
            bucket="bucket",
            mgmt_subnet="172.31.100.0/24",
            mgmt_network="clab-mgmt-lab",
        )

    assert {error["loc"]: error["type"] for error in exc_info.value.errors()}[("schema_version",)] == "literal_error"


def test_runtime_identity_schema_version_defaults_to_v3():
    identity = RuntimeIdentity(
        runtime_id="runtime",
        worker_id="worker-1",
        worker_index=1,
        lab_name="lab",
        topology_id="lab",
        topology_dir=Path("/tmp/lab"),
        bucket="bucket",
        mgmt_subnet="172.31.100.0/24",
        mgmt_network="clab-mgmt-lab",
    )

    assert identity.schema_version == "3"


@pytest.mark.parametrize(
    (
        "name",
        "family",
        "spines",
        "leafs",
        "k",
        "clients",
        "prefix",
        "pingmesh",
        "pps",
        "timeout",
        "deploy_jobs",
        "health_timeout",
    ),
    [
        ("xs", "clos", 2, 2, None, 1, 24, None, 250, 1800, 2, 60),
        ("small", "clos", 2, 4, None, 2, 24, None, 250, 1800, 2, 60),
        ("medium", "clos", 4, 8, None, 2, 24, None, 200, 1800, 2, 60),
        ("large", "clos", 4, 16, None, 4, 24, None, 150, 2700, 1, 180),
        ("xlarge", "clos", 16, 128, None, 1, 23, 16, 100, 3600, 1, 240),
        ("fat-tree-k8", "fat-tree", None, None, 8, 4, 24, 16, 100, 3600, 1, 240),
        ("fat-tree-k12", "fat-tree", None, None, 12, 2, 23, 16, 50, 5400, 1, 300),
    ],
)
def test_scale_profiles_keep_exact_runtime_values(
    name: str,
    family: str,
    spines: int | None,
    leafs: int | None,
    k: int | None,
    clients: int,
    prefix: int,
    pingmesh: int | None,
    pps: int,
    timeout: int,
    deploy_jobs: int,
    health_timeout: int,
):
    profile = get_scale_profile(name)

    assert profile.family == family
    assert profile.num_spines == spines
    assert profile.num_leafs == leafs
    assert profile.fat_tree_k == k
    assert profile.clients_per_attached_switch == clients
    assert profile.management_prefix == prefix
    assert profile.pingmesh_destination_batch_size == pingmesh
    assert profile.traffic_max_pps_per_client == pps
    assert profile.deploy_timeout_seconds == timeout
    assert profile.worker_deploy_parallelism == deploy_jobs
    assert profile.health_timeout_seconds == health_timeout


def test_scale_profile_registry_is_complete_and_rejects_unknown_scales():
    assert supported_scales() == (
        "xs",
        "small",
        "medium",
        "large",
        "xlarge",
        "fat-tree-k8",
        "fat-tree-k12",
    )
    with pytest.raises(ValueError, match="Unknown scale"):
        get_scale_profile("not-a-scale")


def test_fat_tree_profiles_use_bounded_containerlab_parallelism():
    assert get_scale_profile("fat-tree-k8").containerlab_max_workers == 1
    assert get_scale_profile("fat-tree-k12").containerlab_max_workers == 1
    assert get_scale_profile("xlarge").containerlab_max_workers == 16
