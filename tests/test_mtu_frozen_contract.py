import asyncio
from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import (
    BudgetConfig,
    DiagnosabilityConfig,
    TopologyRankerConfig,
)
from examples.agents.diagnostic_harness.evidence.cache import TTLToolCache
from examples.agents.diagnostic_harness.hypotheses import DiagnosabilityGate, HypothesisScorer
from examples.agents.diagnostic_harness.models import Evidence, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.probes.base import ProbeBudget
from examples.agents.diagnostic_harness.topology.interface_ranker import InterfaceRanker
from examples.agents.diagnostic_harness.topology.peer_consistency import PeerConsistencyCollector
from netopsbench.platform.toolkit._core.common import ToolResult

from .test_topology_graph import clos_graph


def _size_evidence(*, failed: bool, source: str, probe_id: str | None = None) -> Evidence:
    return Evidence(
        evidence_id=f"size-{'failed' if failed else 'healthy'}-{source}",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_size_threshold",
        value={"size_dependent_failure": failed},
        source=source,
        timestamp=datetime.now(UTC),
        probe_id=probe_id,
        possible_paths=(
            ("leaf1:Ethernet0--spine1:Ethernet0",),
            ("leaf1:Ethernet4--spine2:Ethernet0",),
        ),
    )


def test_mtu_success_thresholds_are_frozen_and_candidate_recall_is_separate():
    recall = TopologyRankerConfig()
    submit = DiagnosabilityConfig()

    assert recall.candidate_generation_threshold == 0.05
    assert recall.candidate_top_k == 6
    assert submit.minimum_interface_score == 0.15
    assert submit.minimum_interface_margin == 0.05
    assert submit.submit_confidence == 0.75
    assert submit.submit_margin == 0.20


def test_path_scoped_unknown_ecmp_healthy_result_does_not_negate_mtu_failure():
    failed = _size_evidence(failed=True, source="pingmesh_episode")
    healthy_unknown_member = _size_evidence(
        failed=False,
        source="ping_test_df_size_sweep",
        probe_id="mtu-packet-size-sweep",
    )

    hypothesis = HypothesisScorer().score([failed, healthy_unknown_member])["mtu_mismatch"]

    assert hypothesis.score == 5.0
    assert healthy_unknown_member.evidence_id not in hypothesis.contradicting_evidence


def test_low_recall_candidate_cannot_bypass_frozen_final_interface_gate():
    candidate = RankedInterfaceCandidate(
        link_id="leaf1:Ethernet0--spine1:Ethernet0",
        primary_device="leaf1",
        primary_interface="Ethernet0",
        peer_device="spine1",
        peer_interface="Ethernet0",
        score=0.08,
    )
    evidence = [
        _size_evidence(failed=True, source="pingmesh_episode"),
        Evidence(
            evidence_id="direct-mtu-difference",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
            category="configuration_difference",
            value={"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
            source="get_device_interfaces",
            timestamp=datetime.now(UTC),
        ),
    ]
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidate)

    decision = DiagnosabilityGate().analyze(hypotheses, evidence, [candidate])

    assert hypotheses["mtu_mismatch"].probability >= 0.75
    assert not decision.can_submit
    assert "ranked_interface" in decision.missing_requirements


def test_peer_mtu_difference_closes_candidate_rank_and_final_gate():
    graph = clos_graph()
    initial_candidates = InterfaceRanker(graph).rank([_size_evidence(failed=True, source="pingmesh_episode")])
    target = next(
        candidate
        for candidate in initial_candidates
        if candidate.primary_device == "leaf1" and candidate.primary_interface == "Ethernet0"
    )

    class Tools:
        def get_device_interfaces(self, *, device):
            mtu = 1400 if device == "leaf1" else 9100
            return ToolResult(
                success=True,
                data={
                    "interfaces": [
                        {
                            "name": target.primary_interface if device == "leaf1" else target.peer_interface,
                            "admin": "up",
                            "oper": "up",
                            "mtu": mtu,
                        }
                    ]
                },
            )

    class Context:
        tools = Tools()

    budget = ProbeBudget(BudgetConfig())
    peer_evidence = asyncio.run(
        PeerConsistencyCollector().collect(
            Context(),
            candidates=[target],
            budget=budget,
            cache=TTLToolCache(),
            max_candidates=1,
        )
    )
    evidence = [_size_evidence(failed=True, source="pingmesh_episode"), *peer_evidence]
    candidates = InterfaceRanker(graph).rank(evidence)
    hypotheses = HypothesisScorer().score(evidence, interface_candidate=candidates[0])
    decision = DiagnosabilityGate().analyze(hypotheses, evidence, candidates)

    assert candidates[0].primary_device == "leaf1"
    assert candidates[0].primary_interface == "Ethernet0"
    assert any(item.category == "configuration_difference" for item in peer_evidence)
    assert decision.can_submit
    assert decision.confidence >= DiagnosabilityConfig().submit_confidence
    assert decision.margin >= DiagnosabilityConfig().submit_margin
