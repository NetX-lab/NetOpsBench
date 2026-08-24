"""Conservative submission gate for impairment diagnoses."""

from __future__ import annotations

from ..config import DiagnosabilityConfig
from ..evidence.validator import can_support_fault
from ..models import DiagnosabilityDecision, Evidence, Hypothesis, RankedInterfaceCandidate


class DiagnosabilityGate:
    def __init__(self, config: DiagnosabilityConfig | None = None):
        self.config = config or DiagnosabilityConfig()

    def analyze(
        self,
        hypotheses: dict[str, Hypothesis],
        evidence: list[Evidence],
        candidates: list[RankedInterfaceCandidate],
        *,
        conflicts: tuple[tuple[str, str], ...] = (),
        initial_device: str | None = None,
        initial_interface: str | None = None,
        explained_secondary_evidence: frozenset[str] = frozenset(),
    ) -> DiagnosabilityDecision:
        ordered = sorted(hypotheses.values(), key=lambda item: (-item.probability, item.hypothesis_id))
        if not ordered:
            return DiagnosabilityDecision(False, "No hypotheses are available.")
        top = ordered[0]
        second_probability = ordered[1].probability if len(ordered) > 1 else 0.0
        margin = top.probability - second_probability
        missing = list(top.missing_evidence)
        if not top.supporting_evidence:
            # Softmax always has a Top-1, even when every fault hypothesis has
            # zero evidence mass.  Treat that state as unknown rather than a
            # weakly ranked fault.
            missing.append("positive_fault_evidence")
        if top.probability < self.config.submit_confidence:
            missing.append("confidence_threshold")
        if margin < self.config.submit_margin:
            missing.append("hypothesis_margin")
        if conflicts:
            missing.append("unresolved_evidence_conflict")
        if self._has_unexplained_strong_evidence(top, evidence, explained_secondary_evidence):
            # A single-fault answer must explain the strongest direct
            # observations.  This prevents an impairment score from hiding a
            # verified route/config or operational failure while still
            # allowing weak symptoms and downstream consequences to coexist.
            missing.append("unexplained_strong_evidence")

        if top.fault_type == "static_route_misconfig":
            # Static-route closure is semantic and device-scoped; it must not
            # inherit impairment-only payload/interface requirements.
            categories = {item.category for item in evidence if can_support_fault(item)}
            if not categories.intersection({"configured_static_route", "configuration_difference"}):
                missing.append("direct_route_config_evidence")
            if not categories.intersection({"unexpected_next_hop", "observed_routing_entry", "missing_expected_route"}):
                missing.append("prefix_or_nexthop_difference")
            if not top.device:
                missing.append("device_localization")
            consequence = {"route_reachability_consequence", "route_presence"}
            if not categories.intersection(consequence):
                missing.append("independent_route_consequence")
            sources = {
                self._independence_key(item)
                for item in evidence
                if can_support_fault(item) and item.evidence_id in set(top.supporting_evidence)
            }
            if len(sources) < self.config.minimum_independent_evidence_sources:
                missing.append("independent_evidence_sources")
            return DiagnosabilityDecision(
                can_submit=not tuple(dict.fromkeys(missing)),
                reason="Diagnosis is supported and discriminative."
                if not tuple(dict.fromkeys(missing))
                else "Evidence is insufficient for safe submission.",
                top_hypothesis_id=top.hypothesis_id,
                confidence=top.probability,
                margin=margin,
                missing_requirements=tuple(dict.fromkeys(missing)),
            )

        support_ids = set(top.supporting_evidence)
        sources = {
            self._independence_key(item)
            for item in evidence
            if item.evidence_id in support_ids and can_support_fault(item)
        }
        if len(sources) < self.config.minimum_independent_evidence_sources:
            missing.append("independent_evidence_sources")

        candidate = candidates[0] if candidates else None
        if candidate is None or candidate.score < self.config.minimum_interface_score:
            missing.append("ranked_interface")
        else:
            interface_margin = candidate.score - (candidates[1].score if len(candidates) > 1 else 0.0)
            direct_ids = {
                item.evidence_id
                for item in evidence
                if item.entity_type == "interface"
                and item.entity_id == f"{candidate.primary_device}:{candidate.primary_interface}"
                and item.category in {"configuration_difference", "interface_counter_delta"}
                and can_support_fault(item)
            }
            exact_link_observations = [
                item
                for item in evidence
                if candidate.link_id in item.covered_links
                and item.path_observation_confidence >= 1.0
                and can_support_fault(item)
                and (item.probe_id or item.metadata.get("selection") == "access_path_contrast")
            ]
            exact_link_sources = {
                self._independence_key(item)
                for item in exact_link_observations
                if item.category in {"packet_loss_rate", "payload_integrity_failure"}
            }
            cross_validated_link = len(exact_link_sources) >= 2
            corruption_sources = {
                self._independence_key(item)
                for item in evidence
                if item.category == "payload_integrity_failure" and bool(item.value) and can_support_fault(item)
            }
            direct_link_ids = {
                item.evidence_id
                for item in exact_link_observations
                if (
                    (
                        item.category == "packet_loss_rate"
                        and float(item.value or 0.0) >= 0.10
                        and (
                            int(item.metadata.get("rounds") or 0) >= 2
                            or (int(item.metadata.get("sent") or 0) >= 20 and cross_validated_link)
                            or (
                                item.source == "payload_integrity_link_test"
                                and (int(item.metadata.get("sent") or 0) >= 40 or cross_validated_link)
                            )
                        )
                    )
                    or (
                        item.category in {"latency_median", "latency_p95"}
                        and (
                            item.metadata.get("category_anomaly")
                            if "category_anomaly" in item.metadata
                            else (item.metadata.get("absolute_anomaly") or item.metadata.get("relative_anomaly"))
                        )
                    )
                    or (
                        item.category == "packet_size_threshold"
                        and isinstance(item.value, dict)
                        and item.value.get("size_dependent_failure")
                    )
                    or (item.category == "payload_integrity_failure" and bool(item.value))
                    or (
                        item.category == "packet_loss_rate"
                        and item.source == "access_path_contrast"
                        and int(item.metadata.get("abnormal_flow_count") or 0) >= 3
                        and int(item.metadata.get("distinct_remote_endpoints") or 0) >= 2
                        and int(item.metadata.get("cleared_fabric_count") or 0) >= 2
                    )
                )
            }
            endpoint_bound = candidate.layer == "access" or any(
                candidate.link_id in item.covered_links
                and item.metadata.get("fault_endpoint_device") == candidate.primary_device
                and item.metadata.get("fault_endpoint_interface") == candidate.primary_interface
                and can_support_fault(item)
                for item in evidence
            )
            if (
                top.fault_type == "packet_corruption"
                and endpoint_bound
                and not cross_validated_link
                and len(corruption_sources) < 2
                and not direct_ids
            ):
                # One directional checksum observation binds the physical
                # link and a suspect direction, but real endpoint attribution
                # requires a second exact-link method or endpoint-local
                # configuration/counter evidence.  This prevents a controlled
                # probe from being treated as omniscient ground truth.
                missing.append("corruption_endpoint_corroboration")
            if (
                top.fault_type == "high_latency"
                and candidate.layer == "fabric"
                and not endpoint_bound
                and candidate.endpoint_confidence < 0.5
            ):
                # RTT is round-trip evidence: it can bind a physical fabric
                # link but cannot, by itself, tell which endpoint introduced
                # the delay. Do not turn symmetric path evidence into a
                # high-confidence device/interface assertion.
                missing.append("directional_endpoint_evidence")
            direct_path_ids = direct_link_ids if endpoint_bound else set()
            direct_ids |= direct_path_ids
            initial_direct = (
                initial_device == candidate.primary_device and initial_interface == candidate.primary_interface
            )
            if interface_margin < self.config.minimum_interface_margin and not direct_ids and not initial_direct:
                missing.append("interface_margin_or_direct_evidence")
            if not direct_ids and not initial_direct:
                missing.append("direct_interface_evidence")

        unique_missing = tuple(dict.fromkeys(missing))
        return DiagnosabilityDecision(
            can_submit=not unique_missing,
            reason="Diagnosis is supported and discriminative."
            if not unique_missing
            else "Evidence is insufficient for safe submission.",
            top_hypothesis_id=top.hypothesis_id,
            confidence=top.probability,
            margin=margin,
            missing_requirements=unique_missing,
        )

    @staticmethod
    def _independence_key(evidence: Evidence) -> str:
        if evidence.independence_key:
            return evidence.independence_key
        if evidence.probe_id:
            return f"probe:{evidence.source}:{evidence.probe_id}"
        return f"observation:{evidence.source}"

    @staticmethod
    def _has_unexplained_strong_evidence(
        top: Hypothesis,
        evidence: list[Evidence],
        explained_secondary_evidence: frozenset[str] = frozenset(),
    ) -> bool:
        if top.fault_type not in {"packet_loss", "packet_corruption", "mtu_mismatch", "high_latency"}:
            return False
        for item in evidence:
            if item.evidence_id in explained_secondary_evidence:
                continue
            if not can_support_fault(item):
                continue
            semantic = str(item.metadata.get("semantic_family") or "")
            if item.category == "configuration_difference" and semantic in {
                "acl",
                "route_policy",
                "static_route",
            }:
                return True
            if item.category in {"observed_routing_entry", "configured_static_route"} and isinstance(item.value, dict):
                if bool(item.value.get("is_discard")) or item.value.get("difference"):
                    return True
            if item.category in {"interface_admin_state", "interface_oper_state"} and (
                item.value is False or str(item.value).strip().lower() in {"down", "false", "0", "x"}
            ):
                return True
        return False


__all__ = ["DiagnosabilityGate"]
