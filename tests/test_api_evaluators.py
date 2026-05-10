from netopsbench.sdk.evaluators import EvaluatorManager
from netopsbench.sdk.reports import BenchmarkReport
from netopsbench.sdk.scenarios import ScenarioManager
from netopsbench.sdk.types import DiagnosisResult, DiagnosticContext


class CustomScenarioEvaluator:
    def evaluate_scenario(self, *, scenario, diagnosis_results, evaluator="default"):
        return BenchmarkReport(
            report_id=f"custom:{scenario.id}",
            payload={
                "evaluator": evaluator,
                "scenario_id": scenario.id,
                "summary": {"total_cases": len(diagnosis_results), "average_score": 0.25},
                "results": [{"agent": diagnosis_results[0].agent_name, "score": 0.25}],
            },
        )


class EvaluateOnlyEvaluator:
    def evaluate(self, context: DiagnosticContext, result: DiagnosisResult):
        return {
            "scenario_id": context.scenario_id,
            "agent": result.agent_name,
            "score": 0.4,
        }


class ScenarioOnlyEvaluator:
    def evaluate_scenario(self, *, scenario, diagnosis_results, evaluator="default"):
        return {
            "scenario_id": scenario.id,
            "score": 1.0 if diagnosis_results[0].verdict == "fault_detected" else 0.0,
        }


def _make_scenario(*, scenario_id, device, interface, fault_type="link_down"):
    return ScenarioManager().create(
        id=scenario_id,
        name=f"Scenario {scenario_id}",
        scale="xs",
        episodes=[
            {
                "episode_id": f"{scenario_id}-ep1",
                "fault_type": fault_type,
                "target_device": device,
                "target_interface": interface,
            }
        ],
        metadata={"expected_diagnosis": fault_type, "difficulty": "easy"},
    )


def _make_diagnosis(
    *, agent_name="demo-agent", verdict="fault_detected", fault_type="link_down", device="leaf1", interface="Ethernet1"
):
    return DiagnosisResult(
        agent_name=agent_name,
        verdict=verdict,
        confidence=0.9,
        reasoning="diagnosis reasoning",
        findings={
            "fault_type": fault_type,
            "location": {"device": device, "interface": interface},
            "evidence": ["e1"],
        },
        metadata={"tool_calls": [{"name": "show"}], "time_taken_seconds": 1.5},
    )


def test_default_evaluator_accepts_public_diagnosis_results_and_returns_benchmark_report():
    manager = EvaluatorManager()
    scenario = _make_scenario(scenario_id="scenario-1", device="leaf1", interface="Ethernet1")
    diagnosis = _make_diagnosis(device="leaf1", interface="Ethernet1")

    default_evaluator = manager.get("default")

    assert hasattr(default_evaluator, "evaluate_scenario")
    assert hasattr(default_evaluator, "evaluate_suite")

    report = manager.evaluate_scenario(
        scenario=scenario,
        diagnosis_results=[diagnosis],
    )

    assert isinstance(report, BenchmarkReport)
    assert report.report_id == "scenario:scenario-1"
    assert report.payload["evaluator"] == "default"
    assert report.payload["summary"]["total_cases"] == 1
    assert report.payload["summary"]["average_score"] == 1.0
    assert report.payload["detailed_results"][0]["correct_fault_type"] is True
    assert report.payload["detailed_results"][0]["correct_device"] is True


def test_register_returns_normalized_public_evaluator_adapter_and_get_returns_it():
    manager = EvaluatorManager()
    scenario = _make_scenario(scenario_id="scenario-custom", device="leaf2", interface="Ethernet4")
    diagnosis = _make_diagnosis(agent_name="custom-agent", device="leaf2", interface="Ethernet4")

    registered = manager.register("custom", CustomScenarioEvaluator())
    fetched = manager.get("custom")

    assert registered is fetched
    assert hasattr(fetched, "evaluate_scenario")
    assert hasattr(fetched, "evaluate_suite")
    assert "custom" in manager.list()

    report = fetched.evaluate_scenario(
        scenario=scenario,
        diagnosis_results=[diagnosis],
        evaluator="custom",
    )

    assert isinstance(report, BenchmarkReport)
    assert report.report_id == "custom:scenario-custom"
    assert report.payload["results"][0]["agent"] == "custom-agent"


def test_evaluate_only_evaluator_is_normalized_to_public_report_output():
    manager = EvaluatorManager()
    scenario = _make_scenario(scenario_id="scenario-eval-only", device="leaf3", interface="Ethernet7")
    diagnosis = _make_diagnosis(agent_name="eval-only-agent", device="leaf3", interface="Ethernet7")

    adapter = manager.register("simple", EvaluateOnlyEvaluator())

    assert hasattr(adapter, "evaluate_scenario")
    assert hasattr(adapter, "evaluate_suite")

    report = manager.evaluate_scenario(
        scenario=scenario,
        diagnosis_results=[diagnosis],
        evaluator="simple",
    )

    assert isinstance(report, BenchmarkReport)
    assert report.report_id == "scenario:scenario-eval-only"
    assert report.payload["evaluator"] == "simple"
    assert report.payload["summary"]["average_score"] == 0.4
    assert report.payload["results"][0] == {
        "scenario_id": "scenario-eval-only",
        "agent": "eval-only-agent",
        "score": 0.4,
    }


def test_evaluate_only_evaluator_also_supports_suite_evaluation():
    manager = EvaluatorManager()
    manager.register("simple", EvaluateOnlyEvaluator())
    scenarios = [
        _make_scenario(scenario_id="scenario-x", device="leaf1", interface="Ethernet1"),
        _make_scenario(scenario_id="scenario-y", device="leaf2", interface="Ethernet2"),
    ]
    diagnoses = [
        _make_diagnosis(agent_name="agent-x", device="leaf1", interface="Ethernet1"),
        _make_diagnosis(agent_name="agent-y", device="leaf2", interface="Ethernet2"),
    ]

    report = manager.evaluate_suite(
        scenarios=scenarios,
        diagnosis_results=diagnoses,
        evaluator="simple",
    )

    assert isinstance(report, BenchmarkReport)
    assert report.report_id == "suite:2"
    assert report.payload["evaluator"] == "simple"
    assert report.payload["summary"]["total_cases"] == 2
    assert report.payload["summary"]["average_score"] == 0.4
    assert report.payload["results"] == [
        {"scenario_id": "scenario-x", "agent": "agent-x", "score": 0.4},
        {"scenario_id": "scenario-y", "agent": "agent-y", "score": 0.4},
    ]


def test_evaluate_suite_uses_scenario_evaluation_fallback_when_suite_method_missing():
    manager = EvaluatorManager()
    scenarios = [
        _make_scenario(scenario_id="scenario-a", device="leaf1", interface="Ethernet1"),
        _make_scenario(scenario_id="scenario-b", device="leaf2", interface="Ethernet2"),
    ]
    diagnoses = [
        _make_diagnosis(device="leaf1", interface="Ethernet1"),
        _make_diagnosis(verdict="network_healthy", device="leaf2", interface="Ethernet2"),
    ]

    manager.register("scenario-only", ScenarioOnlyEvaluator())

    report = manager.evaluate_suite(
        scenarios=scenarios,
        diagnosis_results=diagnoses,
        evaluator="scenario-only",
    )

    assert isinstance(report, BenchmarkReport)
    assert report.report_id == "suite:2"
    assert report.payload["evaluator"] == "scenario-only"
    assert report.payload["summary"]["total_cases"] == 2
    assert report.payload["summary"]["average_score"] == 0.5
    assert report.payload["results"] == [
        {"scenario_id": "scenario-a", "score": 1.0},
        {"scenario_id": "scenario-b", "score": 0.0},
    ]


def test_unknown_evaluator_name_raises_clean_keyerror_from_get_and_evaluate_methods():
    manager = EvaluatorManager()
    scenario = _make_scenario(scenario_id="scenario-missing", device="leaf9", interface="Ethernet9")
    diagnosis = _make_diagnosis(device="leaf9", interface="Ethernet9")

    for call in (
        lambda: manager.get("missing"),
        lambda: manager.evaluate_scenario(
            scenario=scenario,
            diagnosis_results=[diagnosis],
            evaluator="missing",
        ),
        lambda: manager.evaluate_suite(
            scenarios=[scenario],
            diagnosis_results=[diagnosis],
            evaluator="missing",
        ),
    ):
        try:
            call()
        except KeyError as exc:
            assert str(exc) == "\"Evaluator 'missing' not found\""
        else:
            raise AssertionError("Expected KeyError for unknown evaluator")
