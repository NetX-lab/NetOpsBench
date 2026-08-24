"""Reconcile older path symptoms with newer, exactly scoped observations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from ..models import Evidence, EvidenceOrigin


def reconcile_active_path_coverage(
    evidence: Iterable[Evidence],
    *,
    warning_threshold: float,
    healthy_loss_threshold: float = 0.01,
) -> tuple[Evidence, ...]:
    """Demote a passive path symptom only when every candidate link is clean.

    A single healthy ECMP sample must never disprove an unknown path.  This
    reducer therefore requires fresh, exact-link active observations for the
    complete union of links in all possible paths.  The original observation
    remains in the ledger for planning and audit, but can no longer support a
    fault hypothesis.  A non-fault coverage certificate records the proof.
    """

    items = list(evidence)
    healthy_by_link: dict[str, list[Evidence]] = {}
    for item in items:
        if (
            item.origin is not EvidenceOrigin.ACTIVE_PROBE
            or item.reliability <= 0
            or item.path_observation_confidence < 1.0
            or len(item.covered_links) != 1
        ):
            continue
        healthy = False
        if item.category == "packet_loss_rate":
            try:
                loss_rate = float(item.value)
            except (TypeError, ValueError):
                continue
            sample_count = int(item.metadata.get("sent") or 0)
            rounds = int(item.metadata.get("rounds") or 0)
            healthy = loss_rate <= healthy_loss_threshold and (sample_count >= 20 or rounds >= 2)
        elif item.category == "payload_integrity_failure" and item.value is False:
            healthy = int(item.metadata.get("missing_packets") or 0) == 0
        if healthy:
            healthy_by_link.setdefault(item.covered_links[0], []).append(item)

    reconciled: list[Evidence] = []
    refuted_ids: list[str] = []
    supporting_ids: list[str] = []
    for item in items:
        if item.category != "packet_loss_rate" or not item.supports_submission or not item.possible_paths:
            reconciled.append(item)
            continue
        try:
            anomalous = float(item.value) >= warning_threshold
        except (TypeError, ValueError):
            anomalous = False
        candidate_links = {link_id for path in item.possible_paths for link_id in path}
        if not anomalous or not candidate_links or not candidate_links.issubset(healthy_by_link):
            reconciled.append(item)
            continue
        observations = [observation for link in candidate_links for observation in healthy_by_link[link]]
        if item.timestamp is not None and any(
            observation.timestamp is None or observation.timestamp < item.timestamp for observation in observations
        ):
            reconciled.append(item)
            continue
        refuted_ids.append(item.evidence_id)
        supporting_ids.extend(observation.evidence_id for observation in observations)
        reconciled.append(
            replace(
                item,
                supports_submission=False,
                metadata={
                    **item.metadata,
                    "planning_only": True,
                    "reconciled_by_complete_active_path_coverage": True,
                    "reconciled_by": sorted({observation.evidence_id for observation in observations}),
                },
            )
        )

    if not refuted_ids:
        return tuple(reconciled)

    # A path-group aggregate from an older collector is another view of the
    # same batch, not independent evidence.  Demote it only when no scoped
    # positive path observation remains unresolved.
    unresolved_scoped = any(
        item.category == "packet_loss_rate"
        and item.entity_type == "path"
        and item.supports_submission
        and item.possible_paths
        and _at_or_above(item, warning_threshold)
        for item in reconciled
    )
    if not unresolved_scoped:
        reconciled = [
            replace(
                item,
                supports_submission=False,
                metadata={
                    **item.metadata,
                    "planning_only": True,
                    "reconciled_by_complete_active_path_coverage": True,
                },
            )
            if item.category == "packet_loss_rate"
            and item.entity_type == "path_group"
            and item.supports_submission
            and _at_or_above(item, warning_threshold)
            else item
            for item in reconciled
        ]

    reconciled.append(
        Evidence(
            evidence_id="active-path-coverage-certificate",
            entity_type="failure_domain",
            entity_id="candidate-paths",
            category="coverage_certificate",
            value={
                "refuted_observations": sorted(set(refuted_ids)),
                "healthy_link_observations": sorted(set(supporting_ids)),
                "candidate_links_covered": sorted(healthy_by_link),
            },
            source="evidence_reconciliation",
            timestamp=datetime.now(UTC),
            reliability=1.0,
            origin=EvidenceOrigin.ACTIVE_PROBE,
            independence_key="coverage:active-exact-links",
            supports_submission=False,
            metadata={"coverage_complete": True, "missing_data_is_healthy": False},
        )
    )
    return tuple(reconciled)


def _at_or_above(item: Evidence, threshold: float) -> bool:
    try:
        return float(item.value) >= threshold
    except (TypeError, ValueError):
        return False


__all__ = ["reconcile_active_path_coverage"]
