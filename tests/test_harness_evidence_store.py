from datetime import UTC, datetime, timedelta

import pytest

from examples.agents.diagnostic_harness.evidence import EvidenceStore, can_plan_from, can_support_fault
from examples.agents.diagnostic_harness.models import Evidence


def _evidence(
    evidence_id: str,
    *,
    category: str = "packet_loss_rate",
    entity: str = "leaf1",
    timestamp: datetime | None = None,
    value=0.2,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type="device",
        entity_id=entity,
        category=category,
        value=value,
        source="test-tool",
        timestamp=timestamp,
        raw_reference="raw output that must not enter the LLM summary",
    )


def test_store_queries_recency_freshness_and_conflicts():
    now = datetime.now(UTC)
    older = _evidence("E1", timestamp=now - timedelta(seconds=8), value=0.1)
    newer = _evidence("E2", timestamp=now - timedelta(seconds=2), value=0.2)
    store = EvidenceStore([older, newer])

    assert len(store.get_by_entity("leaf1")) == 2
    assert len(store.get_by_category("packet_loss_rate")) == 2
    assert store.get_recent("leaf1", "packet_loss_rate") == newer
    assert store.has_fresh("leaf1", "packet_loss_rate", max_age_seconds=5, now=now)

    store.mark_conflict("E1", "E2")
    assert store.conflicts == (("E1", "E2"),)


def test_summary_is_structured_bounded_and_omits_raw_reference():
    store = EvidenceStore([_evidence(f"E{index}", value="x" * 500, timestamp=datetime.now(UTC)) for index in range(25)])

    summary = store.summarize_for_llm(max_items=3)

    assert len(summary) == 3
    assert all("raw_reference" not in item for item in summary)
    assert all(len(item["value"]) <= 240 for item in summary)


def test_duplicate_id_with_different_content_is_rejected():
    store = EvidenceStore([_evidence("E1")])

    with pytest.raises(ValueError, match="already exists"):
        store.add(_evidence("E1", value=0.9))


def test_reconciliation_can_revise_existing_items_and_add_derived_evidence():
    original = _evidence("E1", value=0.2)
    revised = Evidence(**{**original.__dict__, "supports_submission": False})
    certificate = _evidence("certificate", category="coverage_certificate", value={"complete": True})
    store = EvidenceStore([original])

    store.apply_reconciliation([revised, certificate])

    assert store.get("E1") == revised
    assert store.get("certificate") == certificate


def test_reconciliation_cannot_drop_existing_audit_evidence():
    store = EvidenceStore([_evidence("E1"), _evidence("E2")])

    with pytest.raises(ValueError, match="omitted existing evidence IDs"):
        store.apply_reconciliation([_evidence("E1")])


def test_tool_errors_and_missing_observations_never_support_faults():
    assert not can_support_fault(_evidence("E1", category="tool_error"))
    assert not can_support_fault(_evidence("E2", category="missing_observation"))
    assert can_support_fault(_evidence("E3", category="packet_loss_rate"))


def test_planning_trust_is_separate_from_submission_trust():
    planning_only = Evidence(
        **{
            **_evidence("E-plan").__dict__,
            "supports_submission": False,
        }
    )
    assert can_plan_from(planning_only)
    assert not can_support_fault(planning_only)
    assert not can_plan_from(_evidence("E-error", category="tool_error"))
