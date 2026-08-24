import json
from datetime import UTC, datetime
from pathlib import Path

from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import Evidence
from examples.agents.diagnostic_harness.normalization.fault_type import FaultTypeNormalizer
from examples.agents.diagnostic_harness.normalization.result import ResultNormalizer
from examples.agents.diagnostic_harness.routing import HardCaseRouter

from .diagnostic_harness_helpers import diagnosis_result, sample_topology

FIXTURE = Path(__file__).parent / "fixtures" / "diagnostic_harness" / "medium_replay.json"


def test_phase1_historical_replay_normalizes_only_safe_aliases():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    normalizer = ResultNormalizer(FaultTypeNormalizer())

    for case in cases:
        outcome = normalizer.normalize(diagnosis_result(case["result"]), topology=sample_topology())
        assert outcome.result.findings["fault_type"] == case["expected_fault_type"], case["name"]
        if "expected_interface" in case:
            assert outcome.result.findings["location"]["interface"] == case["expected_interface"]

    route_policy = next(case for case in cases if case["name"] == "route-policy-semantic-conflict")
    outcome = normalizer.normalize(diagnosis_result(route_policy["result"]), topology=sample_topology())
    assert outcome.result.findings["fault_type"] == "bgp_neighbor_misconfig"


def test_healthy_replay_is_semantically_unchanged():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    normalizer = ResultNormalizer(FaultTypeNormalizer())

    for case in (case for case in cases if case["result"]["verdict"] == "network_healthy"):
        outcome = normalizer.normalize(diagnosis_result(case["result"]), topology=sample_topology())
        assert outcome.result.verdict == "network_healthy"
        assert outcome.result.findings["fault_type"] is None
        assert outcome.result.findings["location"] == {"device": None, "interface": None}


def test_phase2_replay_protects_easy_cases_and_routes_known_hard_cases():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    normalizer = ResultNormalizer(FaultTypeNormalizer())
    router = HardCaseRouter()
    expected_hard = {
        "route-policy-semantic-conflict": "route_policy",
        "packet-loss-missed": "packet_loss",
        "mtu-device-only": "mtu",
        "high-latency-missed": "high_latency",
    }

    for case in cases:
        outcome = normalizer.normalize(diagnosis_result(case["result"]), topology=sample_topology())
        evidence_store = EvidenceStore(
            [
                Evidence(
                    evidence_id=f"E{index}",
                    entity_type="path",
                    entity_id="replay-path",
                    category=item["category"],
                    value=item["value"],
                    source=item["source"],
                    timestamp=datetime.now(UTC),
                )
                for index, item in enumerate(case.get("structured_evidence", []), start=1)
            ]
        )
        decision = router.route(
            result=outcome.result,
            evidence_store=evidence_store,
            normalization_errors=outcome.validation_errors,
        )

        if case["name"] in expected_hard:
            assert not decision.fast_path, case["name"]
            assert decision.family == expected_hard[case["name"]]
        else:
            assert decision.fast_path, case["name"]
