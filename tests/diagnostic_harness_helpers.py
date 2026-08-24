from __future__ import annotations

import json

from examples.agents.diagnostic_harness.normalization.interface import LinkEndpoint, PhysicalLink, TopologyIndex
from netopsbench.sdk.agents import DiagnosisResult


class BaseAgent:
    """Minimal deterministic wrapped agent shared by Harness tests."""

    name = "base"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def diagnose(self, _context):
        self.calls += 1
        return self.result

    def get_capabilities(self):
        return ["base"]


def sample_topology() -> TopologyIndex:
    leaf1 = LinkEndpoint("leaf1", "Ethernet0", ("eth1", "Ethernet0", "e1-1", "ethernet-1/1"))
    spine1 = LinkEndpoint("spine1", "Ethernet0", ("eth1", "Ethernet0", "e1-1", "ethernet-1/1"))
    leaf5 = LinkEndpoint("leaf5", "Ethernet4", ("eth2", "Ethernet4", "e1-2", "ethernet-1/2"))
    spine3 = LinkEndpoint("spine3", "Ethernet4", ("eth2", "Ethernet4", "e1-2", "ethernet-1/2"))
    links = (
        PhysicalLink("leaf1:Ethernet0--spine1:Ethernet0", leaf1, spine1),
        PhysicalLink("leaf5:Ethernet4--spine3:Ethernet4", leaf5, spine3),
    )
    return TopologyIndex(
        devices={"leaf1": "leaf", "leaf5": "leaf", "spine1": "spine", "spine3": "spine"},
        links=links,
        source="test",
    )


def diagnosis_result(payload: dict) -> DiagnosisResult:
    return DiagnosisResult(
        agent_name="replay-agent",
        verdict=payload["verdict"],
        findings={
            "fault_type": payload.get("fault_type"),
            "location": {"device": payload.get("device"), "interface": payload.get("interface")},
            "evidence": payload.get("evidence", []),
        },
        confidence=payload.get("confidence", 0.0),
        reasoning=payload.get("reasoning", ""),
    )


def write_two_leaf_manifest(
    tmp_path,
    *,
    topology_id: str = "test-runtime",
    second_leaf: str = "leaf2",
    second_client: str = "client2",
):
    """Write the common two-leaf Clos fixture with configurable endpoint names."""

    leaves = ("leaf1", second_leaf)
    clients = ("client1", second_client)
    manifest = {
        "schema_version": "3",
        "topology_id": topology_id,
        "name": topology_id,
        "scale": "test",
        "family": "clos",
        "management": {"network": "test", "ipv4_subnet": "172.31.250.0/24"},
        "collector": {"ipv4": "172.31.250.200"},
        "defaults": {"link_mtu": 9232, "sonic_port_mtu": 9100},
        "facts": {
            "num_spines": 1,
            "num_leafs": 2,
            "clients_per_attached_switch": 1,
            "total_clients": 2,
            "total_switches": 3,
        },
        "devices": [
            {"name": "spine1", "role": "spine"},
            *({"name": leaf, "role": "leaf"} for leaf in leaves),
            *(
                {
                    "name": client,
                    "role": "client",
                    "data_ip": f"192.0.2.{index}",
                    "attached_switch": leaf,
                }
                for index, (client, leaf) in enumerate(zip(clients, leaves, strict=True), start=1)
            ),
        ],
        "links": [
            *(
                {
                    "kind": "spine-leaf",
                    "endpoints": [
                        {"device": "spine1", "interface": f"eth{index}"},
                        {"device": leaf, "interface": "eth1"},
                    ],
                }
                for index, leaf in enumerate(leaves, start=1)
            ),
            *(
                {
                    "kind": "client-leaf",
                    "endpoints": [
                        {"device": leaf, "interface": "eth5"},
                        {"device": client, "interface": "eth1"},
                    ],
                }
                for leaf, client in zip(leaves, clients, strict=True)
            ),
        ],
        "routing": {"ecmp_hash_policy_by_role": {"spine": 1, "leaf": 1}},
    }
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path
