import pytest

from examples.agents.diagnostic_harness.config import AdaptiveBudgetConfig, BudgetConfig
from examples.agents.diagnostic_harness.probes.base import ProbeBudget, ProbeBudgetExhausted


def test_semantic_closure_cannot_consume_family_or_collector_reserve():
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12))
    budget.configure_stages(
        family="mtu",
        family_probe_reserve=7,
        evidence_collection_reserve=5,
        semantic_closure_cap=6,
    )

    with budget.use_stage("semantic_closure"):
        with pytest.raises(ProbeBudgetExhausted, match="reserved"):
            budget.reserve_invocation()

    with budget.use_stage("family_probe"):
        for _ in range(7):
            budget.reserve_invocation(packets=2)
    with budget.use_stage("evidence_collection"):
        for _ in range(5):
            budget.reserve_invocation()

    assert budget.tool_calls == 12
    assert budget.allocation_snapshot()["budget_spent_family_probe"] == 7
    assert budget.allocation_snapshot()["budget_spent_evidence_collection"] == 5


def test_semantic_stage_stops_at_its_own_cap():
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12))
    budget.configure_stages(family="runtime_semantic", semantic_closure_cap=2)

    with budget.use_stage("semantic_closure"):
        budget.reserve_invocation()
        budget.reserve_invocation()
        with pytest.raises(ProbeBudgetExhausted, match="semantic_closure"):
            budget.reserve_invocation()


def test_stage_remaining_reports_enforceable_calls_not_global_remainder():
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12))
    budget.configure_stages(family="healthy_verification", triage_cap=7)

    with budget.use_stage("triage"):
        for _ in range(4):
            budget.reserve_invocation()
        assert budget.remaining_tool_calls == 8
        assert budget.remaining_stage_tool_calls == 3


def test_budget_exhaustion_reason_is_explicit_and_tool_errors_do_not_spend_packets():
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=1, max_probe_packets_per_case=20))
    budget.reserve_invocation(packets=20)

    with pytest.raises(ProbeBudgetExhausted, match="tool call budget exhausted"):
        budget.reserve_invocation()
    assert budget.probe_packets == 20


def test_adaptive_budget_is_bounded_traceable_and_extends_only_requested_stage():
    budget = ProbeBudget(
        BudgetConfig(
            max_extra_tool_calls_per_hard_case=12,
            max_active_probes_per_case=6,
            max_probe_packets_per_case=500,
        ),
        adaptive_config=AdaptiveBudgetConfig(
            max_extra_tool_calls=4,
            max_extra_active_probes=2,
            max_extra_probe_packets=80,
        ),
    )
    budget.configure_stages(family="runtime_semantic", semantic_closure_cap=2)
    with budget.use_stage("semantic_closure"):
        budget.reserve_invocation()
        budget.reserve_invocation()
        grant = budget.grant_escalation(
            reason="bounded-route-ambiguity",
            tool_calls=20,
            active_probes=20,
            probe_packets=1000,
            stage="semantic_closure",
        )
        assert grant == {"tool_calls": 4, "active_probes": 2, "probe_packets": 80}
        for _ in range(4):
            budget.reserve_invocation()
        with pytest.raises(ProbeBudgetExhausted, match="semantic_closure"):
            budget.reserve_invocation()

    allocation = budget.allocation_snapshot()
    assert allocation["budget_initial"] == 12
    assert allocation["budget_effective"] == 16
    assert allocation["adaptive_escalation"][0]["reason"] == "bounded-route-ambiguity"
    assert budget.grant_escalation(reason="second-grant", tool_calls=1) == {
        "tool_calls": 0,
        "active_probes": 0,
        "probe_packets": 0,
    }


def test_adaptive_budget_disabled_cannot_expand_limits():
    budget = ProbeBudget(BudgetConfig(), adaptive_config=AdaptiveBudgetConfig(enabled=False))

    assert budget.grant_escalation(reason="not-eligible", tool_calls=4) == {
        "tool_calls": 0,
        "active_probes": 0,
        "probe_packets": 0,
    }
    assert budget.allocation_snapshot()["budget_effective"] == 12
