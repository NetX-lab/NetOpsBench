from netopsbench.evaluator.fault_type_judge import (
    FaultTypeJudgeResult,
    StructuredFaultTypeJudge,
    build_fault_type_judge_request,
    canonicalize_fault_type,
    judge_fault_type_match,
)
from netopsbench.evaluator.scorer import AgentOutput, Evaluator
from netopsbench.sdk.evaluators import EvaluatorManager, create_fault_type_judge_evaluator_adapter
from netopsbench.sdk.scenarios import ScenarioManager
from netopsbench.sdk.types import DiagnosisResult


class RecordingJudge:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def judge(self, request):
        self.requests.append(request)
        return self.result


def _scenario(*, scenario_id="scenario-judge", fault_type="bgp_neighbor_misconfig"):
    return ScenarioManager().create(
        id=scenario_id,
        name="Judge Scenario",
        scale="xs",
        episodes=[
            {
                "episode_id": f"{scenario_id}-ep1",
                "fault_type": fault_type,
                "target_device": "leaf1",
                "target_interface": "Ethernet1",
            }
        ],
        metadata={"expected_diagnosis": fault_type, "difficulty": "easy"},
    )


def _diagnosis(*, fault_type="BGP peer AS mismatch"):
    return DiagnosisResult(
        agent_name="judge-agent",
        verdict="fault_detected",
        confidence=0.9,
        reasoning="The BGP peer has a remote AS mismatch and the session is not established.",
        findings={
            "fault_type": fault_type,
            "location": {"device": "leaf1", "interface": "Ethernet1"},
            "evidence": ["show bgp summary reports an idle peer"],
        },
    )


def test_canonicalize_fault_type_preserves_legacy_aliases():
    assert canonicalize_fault_type("link failure") == "link_down"
    assert canonicalize_fault_type("static_route_misconfiguration") == "static_route_misconfig"
    assert canonicalize_fault_type("acl_misconfiguration") == "acl_misconfig"


def test_exact_canonical_match_uses_fast_path_without_calling_judge():
    judge = RecordingJudge(
        FaultTypeJudgeResult(
            canonical_agent_fault_type="packet_loss",
            canonical_ground_truth_fault_type="link_down",
            is_match=False,
            confidence=0.1,
            reasoning="should not be called",
        )
    )

    is_match, details = judge_fault_type_match(
        judge=judge,
        agent_fault_type="link failure",
        ground_truth_fault_type="link_down",
    )

    assert is_match is True
    assert details["mode"] == "deterministic"
    assert details["canonical_agent_fault_type"] == "link_down"
    assert judge.requests == []


def test_structured_fault_type_judge_accepts_mapping_results():
    def judge_fn(request):
        return {
            "canonical_agent_fault_type": "bgp_neighbor_misconfig",
            "canonical_ground_truth_fault_type": request.canonical_ground_truth_fault_type,
            "is_match": True,
            "confidence": 0.92,
            "reasoning": "remote AS mismatch is a BGP neighbor misconfiguration",
        }

    judge = StructuredFaultTypeJudge(judge_fn, model="judge-model")
    request = build_fault_type_judge_request(
        agent_fault_type="BGP peer AS mismatch",
        ground_truth_fault_type="bgp_neighbor_misconfig",
        agent_reasoning="BGP peer has wrong remote AS.",
    )

    result = judge.judge(request)

    assert result.is_match is True
    assert result.canonical_agent_fault_type == "bgp_neighbor_misconfig"
    assert result.judge_model == "judge-model"


def test_evaluator_uses_judge_for_semantic_fault_type_match():
    judge = RecordingJudge(
        FaultTypeJudgeResult(
            canonical_agent_fault_type="bgp_neighbor_misconfig",
            canonical_ground_truth_fault_type="bgp_neighbor_misconfig",
            is_match=True,
            confidence=0.93,
            reasoning="The agent described a BGP peer AS/session configuration issue.",
            judge_model="mock-judge",
        )
    )
    evaluator = Evaluator(fault_type_judge=judge)

    result = evaluator.evaluate(
        AgentOutput(
            verdict="fault_detected",
            fault_type="BGP peer AS mismatch",
            location={"device": "leaf1", "interface": "Ethernet1"},
            reasoning="The BGP peer has the wrong remote AS.",
            evidence=["BGP session is Idle"],
        ),
        {"fault_type": "bgp_neighbor_misconfig", "location": {"device": "leaf1", "interface": "Ethernet1"}},
        "case-1",
    )

    assert result.correct_fault_type is True
    assert result.details["fault_type_judgment"]["mode"] == "llm_judge"
    assert result.details["fault_type_judgment"]["confidence"] == 0.93
    assert len(judge.requests) == 1


def test_taxonomy_violation_forces_mismatch():
    judgment = FaultTypeJudgeResult(
        canonical_agent_fault_type="generic_routing_problem",
        canonical_ground_truth_fault_type="blackhole_route",
        is_match=True,
        confidence=0.99,
        reasoning="invalid taxonomy output",
    )

    assert judgment.taxonomy_violation is True
    assert judgment.is_match is False


def test_judge_true_is_rejected_when_canonical_agent_type_differs_from_ground_truth():
    judge = RecordingJudge(
        FaultTypeJudgeResult(
            canonical_agent_fault_type="packet_loss",
            canonical_ground_truth_fault_type="mtu_mismatch",
            is_match=True,
            confidence=0.95,
            reasoning="overly broad judge output",
        )
    )

    is_match, details = judge_fault_type_match(
        judge=judge,
        agent_fault_type="packet drops",
        ground_truth_fault_type="mtu_mismatch",
    )

    assert is_match is False
    assert details["agent_canonical_mismatch"] is True


def test_sdk_fault_type_judge_adapter_can_be_registered():
    judge = RecordingJudge(
        FaultTypeJudgeResult(
            canonical_agent_fault_type="bgp_neighbor_misconfig",
            canonical_ground_truth_fault_type="bgp_neighbor_misconfig",
            is_match=True,
            confidence=0.9,
            reasoning="semantic match",
        )
    )
    manager = EvaluatorManager()
    manager.register("llm-fault-type-v1", create_fault_type_judge_evaluator_adapter(judge))

    report = manager.evaluate_scenario(
        scenario=_scenario(),
        diagnosis_results=[_diagnosis()],
        evaluator="llm-fault-type-v1",
    )

    detailed = report.payload["detailed_results"][0]
    assert report.payload["evaluator"] == "llm-fault-type-v1"
    assert detailed["correct_fault_type"] is True
    assert detailed["details"]["fault_type_judgment"]["mode"] == "llm_judge"


def test_create_judge_from_env_returns_none_when_disabled():
    from netopsbench.config import FaultTypeJudgeConfig
    from netopsbench.evaluator.fault_type_judge import create_judge_from_env

    cfg = FaultTypeJudgeConfig(enabled=False)
    assert create_judge_from_env(cfg) is None


def test_create_judge_from_env_returns_judge_when_enabled():
    import sys
    from unittest.mock import MagicMock

    from netopsbench.config import FaultTypeJudgeConfig
    from netopsbench.evaluator.fault_type_judge import StructuredFaultTypeJudge, create_judge_from_env

    mock_chat_openai_cls = MagicMock()
    mock_llm_instance = MagicMock()
    mock_chat_openai_cls.return_value = mock_llm_instance
    mock_structured_llm = MagicMock()
    mock_llm_instance.with_structured_output.return_value = mock_structured_llm

    mock_langchain_openai = MagicMock()
    mock_langchain_openai.ChatOpenAI = mock_chat_openai_cls

    original = sys.modules.get("langchain_openai")
    sys.modules["langchain_openai"] = mock_langchain_openai
    try:
        cfg = FaultTypeJudgeConfig(enabled=True, model="test-model", api_key="sk-test", base_url=None)
        judge = create_judge_from_env(cfg)
    finally:
        if original is None:
            sys.modules.pop("langchain_openai", None)
        else:
            sys.modules["langchain_openai"] = original

    assert isinstance(judge, StructuredFaultTypeJudge)
    assert judge.model == "test-model"
    mock_chat_openai_cls.assert_called_once_with(model="test-model", temperature=0, api_key="sk-test")
    mock_llm_instance.with_structured_output.assert_called_once()
