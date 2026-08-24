"""Configurable physical-link and evaluator-interface ranking."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

from ..config import TopologyRankerConfig
from ..evidence.validator import can_plan_from, can_support_fault
from ..models import Evidence, EvidenceOrigin, RankedInterfaceCandidate
from .graph import TopologyGraph
from .path_analysis import analyze_path_evidence
from .semantics import attachment_from_metadata


class InterfaceRanker:
    def __init__(
        self,
        graph: TopologyGraph,
        config: TopologyRankerConfig | None = None,
        *,
        family: str | None = None,
    ):
        self.graph = graph
        self.config = config or TopologyRankerConfig()
        self.family = family

    def rank(
        self,
        evidence: list[Evidence],
        *,
        initial_device: str | None = None,
        initial_interface: str | None = None,
        _include_access_path_candidates: bool = False,
    ) -> list[RankedInterfaceCandidate]:
        analysis = analyze_path_evidence(self.graph, evidence, config=self.config, family=self.family)
        concentrated_access_links = self._concentrated_access_links(evidence)
        candidates: list[RankedInterfaceCandidate] = []
        positive_weight = max(
            1.0,
            self.config.abnormal_path_weight
            + self.config.active_probe_weight
            + self.config.counter_anomaly_weight
            + self.config.configuration_difference_weight
            + self.config.initial_device_bonus
            + self.config.initial_interface_bonus,
        )
        initial_link = (
            self.graph.endpoint_link(initial_device, initial_interface)
            if initial_device and initial_interface
            else None
        )
        for link in self.graph.diagnosable_links():
            link_id = link.link_id
            components = {
                "abnormal_path": analysis.abnormal_support.get(link_id, 0.0),
                "healthy_path": analysis.healthy_support.get(link_id, 0.0),
                "active_probe": analysis.active_probe_support.get(link_id, 0.0),
                "counter_anomaly": analysis.counter_support.get(link_id, 0.0),
                "configuration_difference": analysis.configuration_support.get(link_id, 0.0),
            }
            raw_score = (
                self.config.abnormal_path_weight * components["abnormal_path"]
                + self.config.healthy_path_weight * components["healthy_path"]
                + self.config.active_probe_weight * components["active_probe"]
                + self.config.counter_anomaly_weight * components["counter_anomaly"]
                + self.config.configuration_difference_weight * components["configuration_difference"]
            )
            roles = {
                self.graph.roles.get(link.physical.endpoint_a.device),
                self.graph.roles.get(link.physical.endpoint_b.device),
            }
            endpoints = (link.physical.endpoint_a, link.physical.endpoint_b)
            direct_endpoint_support = max(
                analysis.causal_endpoint_support.get(
                    (endpoint.device, endpoint.canonical_interface),
                    0.0,
                )
                for endpoint in endpoints
            )
            # End-to-end path evidence necessarily crosses both client access
            # edges, so admitting those edges into the normal Top-K would
            # crowd out every ECMP fabric member.  Access links enter the
            # submission set only after direct interface/config/counter
            # evidence (or an exact initial interface) binds them.  They may
            # still be explored separately by the peer/config collector.
            access_link = "client" in roles
            direct_access_support = bool(
                components["counter_anomaly"] > 0
                or components["configuration_difference"] > 0
                or (initial_link is not None and link_id == initial_link.link_id)
                or direct_endpoint_support >= 5.0
            )
            if (
                access_link
                and not direct_access_support
                and link_id not in concentrated_access_links
                and not _include_access_path_candidates
            ):
                continue
            if initial_device and any(endpoint.device == initial_device for endpoint in endpoints):
                raw_score += self.config.initial_device_bonus
            if initial_link is not None and link_id == initial_link.link_id:
                raw_score += self.config.initial_interface_bonus
            # A directional active probe identifies an endpoint, not merely a
            # path.  Give that direct observation the same bounded ordering
            # bonus as an exact initial interface so a larger topology's many
            # indirect ECMP paths cannot outrank it.  Submission still goes
            # through the unchanged interface/confidence/margin gates.
            if direct_endpoint_support >= 5.0:
                raw_score += self.config.initial_interface_bonus
            score = min(1.0, max(0.0, raw_score / positive_weight))
            primary, peer = self._ordered_endpoints(
                endpoints,
                causal_endpoint_support=analysis.causal_endpoint_support,
                symptom_endpoint_support=analysis.symptom_endpoint_support,
                initial_device=initial_device,
                initial_interface=initial_interface,
            )
            candidates.append(
                RankedInterfaceCandidate(
                    link_id=link_id,
                    primary_device=primary.device,
                    primary_interface=primary.canonical_interface,
                    peer_device=peer.device,
                    peer_interface=peer.canonical_interface,
                    score=score,
                    layer="access" if access_link else "fabric",
                    endpoint_confidence=(
                        1.0
                        if analysis.causal_endpoint_support.get((primary.device, primary.canonical_interface), 0.0)
                        >= 5.0
                        else 0.7
                        if initial_device == primary.device and initial_interface == primary.canonical_interface
                        else min(
                            0.49,
                            analysis.symptom_endpoint_support.get((primary.device, primary.canonical_interface), 0.0)
                            / 5.0,
                        )
                    ),
                    components=components,
                    evidence_ids=tuple(sorted(analysis.evidence_ids.get(link_id, ()))),
                )
            )
        ordered = sorted(candidates, key=lambda item: (-item.score, item.link_id))
        threshold = (
            self.config.minimum_candidate_score
            if self.config.minimum_candidate_score is not None
            else self.config.candidate_generation_threshold
        )
        selected = [item for item in ordered if item.score >= threshold]
        # Backfill positive path-supported candidates up to Top-K. This keeps
        # ECMP members in the investigation set when suspicion is diluted
        # across several equal-cost paths.
        if len(selected) < self.config.candidate_top_k:
            selected_ids = {item.link_id for item in selected}
            selected.extend(
                item
                for item in ordered
                if item.link_id not in selected_ids
                and item.score > 0
                and (
                    item.components["abnormal_path"] > 0
                    or item.components["active_probe"] > 0
                    or item.components["counter_anomaly"] > 0
                    or item.components["configuration_difference"] > 0
                )
            )
        return selected[: self.config.candidate_top_k]

    def expansion_candidates(
        self,
        evidence: list[Evidence],
        *,
        selected: list[RankedInterfaceCandidate],
        max_candidates: int,
        initial_device: str | None = None,
        initial_interface: str | None = None,
    ) -> list[RankedInterfaceCandidate]:
        """Return probe-only ECMP diversity candidates beyond normal Top-K.

        These candidates never bypass ``candidate_top_k`` for submission. They
        are only safe targets for a bounded active probe or read-only peer
        collector. New direct evidence is then fed through the normal ranker
        and gate again.
        """
        limit = max(0, int(max_candidates))
        if limit == 0:
            return []
        all_config = replace(
            self.config,
            candidate_top_k=max(1, len(self.graph.diagnosable_links())),
            minimum_candidate_score=0.0,
        )
        ordered = InterfaceRanker(self.graph, all_config, family=self.family).rank(
            evidence,
            initial_device=initial_device,
            initial_interface=initial_interface,
            _include_access_path_candidates=True,
        )
        local_domain = self.failure_domain_link_ids(evidence)
        if local_domain:
            # Search the affected attachment/pod before unrelated fabric
            # links. Keep the remaining candidates as a fallback: locality is
            # a planning order, never invented fault evidence.
            ordered.sort(key=lambda item: (0 if item.link_id in local_domain else 1, -item.score, item.link_id))
        by_id = {item.link_id: item for item in ordered}
        order = {item.link_id: index for index, item in enumerate(ordered)}
        directly_observed = {
            link_id
            for item in evidence
            if item.probe_id
            and item.reliability > 0
            and can_support_fault(item)
            and item.path_observation_confidence >= 1.0
            and self._observes_ranked_family(item)
            for link_id in item.covered_links
        }
        selected_ids = {item.link_id for item in selected}
        retry_unobserved_selected = self.family in {"packet_loss", "packet_corruption", "high_latency"}
        excluded = set(directly_observed)
        if not retry_unobserved_selected:
            excluded.update(selected_ids)
        expanded: list[RankedInterfaceCandidate] = []

        expansion_seeds = sorted(
            (item for item in evidence if self._supports_adaptive_expansion(item)),
            key=self._expansion_seed_priority,
        )

        # A normal Top-K candidate is only "covered" after a successful exact
        # link observation.  Selection alone is not execution: a timeout or a
        # stage-budget failure must leave that candidate eligible for the
        # bounded recovery probe.
        if retry_unobserved_selected:
            for candidate in sorted(
                selected,
                key=lambda item: (0 if item.link_id in local_domain else 1, -item.score, item.link_id),
            ):
                if candidate.link_id in excluded or candidate.link_id not in by_id:
                    continue
                expanded.append(by_id[candidate.link_id])
                if len(expanded) >= limit:
                    return expanded

        # An attachment-wide Pingmesh concentration may come from one of its client
        # access links.  First clear every fabric adjacency with direct link
        # probes; when those controls are healthy, spend the bounded expansion
        # on the attachment switch's access edges instead of re-probing healthy uplinks.
        cleared_attachments = self._attachments_with_cleared_fabric(evidence)
        for link_id in sorted(
            self._concentrated_attachment_access_links(evidence, allowed_attachments=cleared_attachments),
            key=lambda candidate_id: (order.get(candidate_id, 10**9), candidate_id),
        ):
            if link_id in excluded or link_id not in by_id:
                continue
            expanded.append(by_id[link_id])
            if len(expanded) >= limit:
                return expanded

        # A size threshold can originate on a client-facing MTU boundary.
        # Access links are excluded from normal end-to-end ranking because
        # every flow crosses them, but the bounded peer/config collector must
        # still inspect the source/destination access layer before concluding
        # that all ECMP fabric members are healthy.
        mtu_seeds = [item for item in expansion_seeds if item.category == "packet_size_threshold"]
        # Prefer the active size sweep over the many passive Pingmesh rows. A
        # single bounded sweep identifies one concrete endpoint pair; unioning
        # every passive MTU symptom on a large topology consumes the whole
        # expansion allowance on unrelated client access links.
        strongest_mtu_seed = mtu_seeds[0] if mtu_seeds else None
        mtu_path_ids = {
            link_id
            for path in (strongest_mtu_seed.possible_paths if strongest_mtu_seed is not None else ())
            for link_id in path
        }
        for link_id in sorted(mtu_path_ids, key=lambda candidate_id: (order.get(candidate_id, 10**9), candidate_id)):
            link = self.graph.link(link_id)
            if link is None or "client" not in {
                self.graph.roles.get(link.physical.endpoint_a.device),
                self.graph.roles.get(link.physical.endpoint_b.device),
            }:
                continue
            if link_id in excluded or link_id not in by_id:
                continue
            expanded.append(by_id[link_id])
            if len(expanded) >= limit:
                return expanded

        # Prefer links that distinguish one unresolved ECMP member from the
        # others. Shared ingress/egress edges do not improve member coverage.
        for item in expansion_seeds:
            paths = [set(path) for path in item.possible_paths if path]
            if len(paths) < 2:
                continue
            shared = set.intersection(*paths)
            for path in paths:
                choices = [
                    link_id
                    for link_id in path - shared
                    if link_id in by_id
                    and link_id not in excluded
                    and all(candidate.link_id != link_id for candidate in expanded)
                ]
                if not choices:
                    continue
                link_id = min(choices, key=lambda candidate_id: (order[candidate_id], candidate_id))
                expanded.append(by_id[link_id])
                if len(expanded) >= limit:
                    return expanded

        # If path signatures overlap, fill only from candidates with positive
        # structured support; never probe arbitrary zero-score fabric links.
        fill_order = sorted(
            ordered,
            key=lambda candidate: (
                0 if candidate.layer == "fabric" else 1,
                order[candidate.link_id],
            ),
        )
        local_fill = [candidate for candidate in fill_order if candidate.link_id in local_domain]
        nonlocal_fill = [candidate for candidate in fill_order if candidate.link_id not in local_domain]
        remaining_slots = max(0, limit - len(expanded))
        if remaining_slots and len(local_fill) > remaining_slots:
            # Equal-score candidates in a very wide local domain otherwise
            # inherit lexical device ordering, permanently starving the tail
            # of the domain. Spread the bounded frontier over the whole cut.
            # Strongly differentiated candidates retain their score order.
            score_span = max(item.score for item in local_fill) - min(item.score for item in local_fill)
            if score_span <= 0.02:
                local_fill = self._stratified_order(local_fill, remaining_slots)
        fill_order = [*local_fill, *nonlocal_fill]
        for candidate in fill_order:
            if candidate.link_id in excluded or any(item.link_id == candidate.link_id for item in expanded):
                continue
            if candidate.score <= 0 or not any(value > 0 for value in candidate.components.values()):
                continue
            expanded.append(candidate)
            if len(expanded) >= limit:
                break
        return expanded

    @staticmethod
    def _stratified_order(
        candidates: list[RankedInterfaceCandidate],
        frontier_size: int,
    ) -> list[RankedInterfaceCandidate]:
        """Place representatives from the full ordered domain first."""
        size = max(0, int(frontier_size))
        if size == 0 or len(candidates) <= size:
            return list(candidates)
        if size == 1:
            indices = [len(candidates) // 2]
        else:
            indices = [round(index * (len(candidates) - 1) / (size - 1)) for index in range(size)]
        representative_ids = {candidates[index].link_id for index in indices}
        representatives = [candidates[index] for index in indices]
        remainder = [item for item in candidates if item.link_id not in representative_ids]
        return [*representatives, *remainder]

    def failure_domain_link_ids(self, evidence: list[Evidence], *, max_attachments: int = 2) -> set[str]:
        """Return a topology-local search domain from public path symptoms.

        For Clos this is the two endpoint attachment cuts.  For a Fat-tree it
        additionally includes the adjacent aggregation layer, which covers the
        affected pods without enumerating unrelated pods or clients.  The
        method only orders candidate collection and cannot support submission.
        """
        counts: Counter[str] = Counter()
        for item in evidence:
            if not can_plan_from(item) or item.entity_type != "path":
                continue
            abnormal = False
            if item.category == "packet_loss_rate":
                try:
                    abnormal = float(item.value) >= 0.10
                except (TypeError, ValueError):
                    abnormal = False
            elif item.category in {"latency_median", "latency_p95"}:
                abnormal = bool(
                    item.metadata.get("category_anomaly")
                    or item.metadata.get("absolute_anomaly")
                    or item.metadata.get("relative_anomaly")
                )
            elif item.category == "packet_size_threshold":
                abnormal = isinstance(item.value, dict) and bool(item.value.get("size_dependent_failure"))
            elif item.category == "payload_integrity_failure":
                abnormal = bool(item.value)
            if not abnormal:
                continue
            for side in ("source", "destination"):
                attachment = self.graph.resolve_node(attachment_from_metadata(item.metadata, side))
                if attachment and self.graph.is_attachment_device(attachment):
                    counts[attachment] += 1
        attachments = [
            device for device, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:max_attachments]
        ]
        domain: set[str] = set()
        for attachment in attachments:
            for link in self.graph.fabric_links(attachment):
                domain.add(link.link_id)
                endpoints = (link.physical.endpoint_a.device, link.physical.endpoint_b.device)
                peer = endpoints[1] if endpoints[0] == attachment else endpoints[0]
                # An aggregation switch is pod-local in a Fat-tree; its core
                # adjacencies belong to the same failure domain. A Clos spine
                # is global, so expanding through it would reintroduce every
                # unrelated leaf and defeat hierarchical localization.
                if self.graph.roles.get(peer) == "agg":
                    domain.update(item.link_id for item in self.graph.fabric_links(peer))
        return domain

    def healthy_link_isolation_frontier(
        self,
        evidence: list[Evidence],
        *,
        warning_threshold: float,
        max_candidates: int,
        preferred_device: str | None = None,
    ) -> list[RankedInterfaceCandidate]:
        """Reuse one exact attachment-switch fabric cut across probe methods.

        Changing from link ping to payload verification must not discard the
        already isolated physical links and start a new topology-wide search.
        This returns the best-covered attachment's exact healthy isolation links for
        bounded follow-up only; it does not alter submission ranking.
        """
        limit = max(0, int(max_candidates))
        if limit == 0:
            return []
        evidence_ids_by_link: dict[str, list[str]] = {}
        network_link_ids = {link.link_id for link in self.graph.network_links()}
        for item in evidence:
            if (
                item.category != "packet_loss_rate"
                or item.origin is not EvidenceOrigin.ACTIVE_PROBE
                or item.path_observation_confidence < 1.0
                or item.metadata.get("selection") != "link_isolation"
            ):
                continue
            try:
                healthy = float(item.value) < warning_threshold
            except (TypeError, ValueError):
                healthy = False
            if not healthy:
                continue
            for link_id in item.covered_links:
                if link_id in network_link_ids:
                    evidence_ids_by_link.setdefault(link_id, []).append(item.evidence_id)

        links_by_attachment: dict[str, set[str]] = {}
        for link_id in evidence_ids_by_link:
            link = self.graph.link(link_id)
            if link is None:
                continue
            for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b):
                if self.graph.is_attachment_device(endpoint.device):
                    links_by_attachment.setdefault(endpoint.device, set()).add(link_id)
        eligible = {
            attachment: sorted(link_ids) for attachment, link_ids in links_by_attachment.items() if len(link_ids) >= 2
        }
        if not eligible:
            return []
        preferred = self.graph.resolve_node(preferred_device)
        attachment = min(
            eligible,
            key=lambda item: (0 if item == preferred else 1, -len(eligible[item]), item),
        )

        candidates: list[RankedInterfaceCandidate] = []
        for link_id in eligible[attachment][:limit]:
            link = self.graph.link(link_id)
            if link is None:
                continue
            endpoints = (link.physical.endpoint_a, link.physical.endpoint_b)
            primary = next(endpoint for endpoint in endpoints if endpoint.device == attachment)
            peer = next(endpoint for endpoint in endpoints if endpoint.device != attachment)
            candidates.append(
                RankedInterfaceCandidate(
                    link_id=link_id,
                    primary_device=primary.device,
                    primary_interface=primary.canonical_interface,
                    peer_device=peer.device,
                    peer_interface=peer.canonical_interface,
                    score=0.0,
                    layer="fabric",
                    evidence_ids=tuple(sorted(evidence_ids_by_link[link_id])),
                )
            )
        return candidates

    def access_isolation_frontier(
        self,
        evidence: list[Evidence],
        *,
        fabric_frontier: list[RankedInterfaceCandidate],
        max_candidates: int,
    ) -> list[RankedInterfaceCandidate]:
        """Return access edges behind an attachment switch whose fabric cut is healthy.

        Once every directly attached fabric link has an exact healthy active
        observation, repeating another probe method across the same cut adds
        little information.  The remaining bounded search domain is the
        attachment's client-facing edges. This method only plans probes; it does not
        turn topology membership into fault evidence.
        """
        limit = max(0, int(max_candidates))
        if limit == 0 or not fabric_frontier:
            return []
        attachment_counts: Counter[str] = Counter()
        for candidate in fabric_frontier:
            for device in (candidate.primary_device, candidate.peer_device):
                if self.graph.is_attachment_device(device):
                    attachment_counts[device] += 1
        if not attachment_counts:
            return []
        attachment = min(attachment_counts, key=lambda item: (-attachment_counts[item], item))
        all_config = replace(
            self.config,
            candidate_top_k=max(1, len(self.graph.diagnosable_links())),
            minimum_candidate_score=0.0,
        )
        candidates = InterfaceRanker(self.graph, all_config, family=self.family).rank(
            evidence,
            _include_access_path_candidates=True,
        )
        concentrated = self._concentrated_access_links(evidence)
        access = [
            candidate
            for candidate in candidates
            if candidate.layer == "access" and candidate.primary_device == attachment
        ]
        return sorted(
            access,
            key=lambda item: (
                0 if item.link_id in concentrated else 1,
                -item.score,
                item.link_id,
            ),
        )[:limit]

    def access_path_contrast_evidence(
        self,
        evidence: list[Evidence],
        *,
        warning_threshold: float,
    ) -> tuple[Evidence, ...]:
        """Bind persistent endpoint loss to an access edge by elimination.

        This is deliberately stricter than candidate generation: at least
        three abnormal flows from two remote endpoints must share one client,
        every fabric adjacency of its attachment switch must have an exact healthy active
        probe, and a checksum probe on the access edge must exclude payload
        corruption.  The result is a traceable composite observation, not a
        case-specific score bonus.
        """
        cleared_attachments = self._attachments_with_cleared_fabric(evidence)
        if not cleared_attachments:
            return ()
        results: list[Evidence] = []
        for link_id in sorted(self._concentrated_access_links(evidence)):
            link = self.graph.link(link_id)
            if link is None:
                continue
            endpoints = (link.physical.endpoint_a, link.physical.endpoint_b)
            attachment_endpoint = next(
                (endpoint for endpoint in endpoints if self.graph.is_attachment_device(endpoint.device)),
                None,
            )
            client_endpoint = next(
                (endpoint for endpoint in endpoints if self.graph.roles.get(endpoint.device) == "client"),
                None,
            )
            if (
                attachment_endpoint is None
                or client_endpoint is None
                or attachment_endpoint.device not in cleared_attachments
            ):
                continue
            abnormal: list[Evidence] = []
            remote_endpoints: set[str] = set()
            for item in evidence:
                if item.category != "packet_loss_rate" or not can_support_fault(item):
                    continue
                try:
                    if float(item.value) < warning_threshold:
                        continue
                except (TypeError, ValueError):
                    continue
                raw = (
                    str(item.metadata.get("source") or item.metadata.get("src_name") or ""),
                    str(item.metadata.get("destination") or item.metadata.get("dst_name") or ""),
                )
                if not all(raw) and "--" in item.entity_id:
                    raw = tuple(item.entity_id.split("--", 1))
                resolved = tuple(self.graph.resolve_node(value) or value for value in raw)
                if client_endpoint.device not in resolved:
                    continue
                abnormal.append(item)
                remote_endpoints.update(value for value in resolved if value != client_endpoint.device)
            if len(abnormal) < 3 or len(remote_endpoints) < 2:
                continue
            clean_integrity = [
                item
                for item in evidence
                if item.category == "payload_integrity_failure"
                and item.value is False
                and link_id in item.covered_links
                and item.path_observation_confidence >= 1.0
                and can_support_fault(item)
            ]
            if not clean_integrity:
                continue
            fabric_ids = {
                candidate.link_id
                for candidate in self.graph.network_links()
                if attachment_endpoint.device
                in {
                    candidate.physical.endpoint_a.device,
                    candidate.physical.endpoint_b.device,
                }
            }
            cleared_ids = {
                observed_link
                for item in evidence
                if item.category == "packet_loss_rate"
                and item.origin is EvidenceOrigin.ACTIVE_PROBE
                and item.path_observation_confidence >= 1.0
                and item.metadata.get("selection") == "link_isolation"
                and can_support_fault(item)
                and float(item.value or 0.0) < warning_threshold
                for observed_link in item.covered_links
            }
            if len(fabric_ids) < 2 or not fabric_ids.issubset(cleared_ids):
                continue
            derived_from = tuple(
                dict.fromkeys(
                    [*(item.evidence_id for item in abnormal), *(item.evidence_id for item in clean_integrity)]
                )
            )
            results.append(
                Evidence(
                    evidence_id=f"access-path-contrast:{link_id}",
                    entity_type="path",
                    entity_id=f"{attachment_endpoint.device}--{client_endpoint.device}",
                    category="packet_loss_rate",
                    value=max(float(item.value) for item in abnormal),
                    source="access_path_contrast",
                    timestamp=datetime.now(UTC),
                    reliability=0.8,
                    probe_id="access-path-contrast",
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"composite:access-path-contrast:{link_id}",
                    observed_path=(link_id,),
                    possible_paths=((link_id,),),
                    covered_links=(link_id,),
                    path_observation_confidence=1.0,
                    metadata={
                        "selection": "access_path_contrast",
                        "abnormal_flow_count": len(abnormal),
                        "distinct_remote_endpoints": len(remote_endpoints),
                        "cleared_fabric_count": len(fabric_ids),
                        "fault_endpoint_device": attachment_endpoint.device,
                        "fault_endpoint_interface": attachment_endpoint.canonical_interface,
                        "derived_from": derived_from,
                        "validation_mode": "endpoint_concentration_plus_healthy_fabric_cut",
                    },
                )
            )
        return tuple(results)

    def concentrated_access_links(self, evidence: list[Evidence]) -> set[str]:
        """Return client edges justified by repeated endpoint observations."""
        return self._concentrated_access_links(evidence)

    def _concentrated_access_links(self, evidence: list[Evidence]) -> set[str]:
        """Admit only access edges implicated by repeated endpoint symptoms.

        Every end-to-end path contains two access edges, so admitting all of
        them destroys fabric Top-K recall.  Conversely, excluding all access
        edges makes a real client-facing fault impossible to localize.  A
        client edge is therefore admitted only when at least two independent
        abnormal observations share that client and it accounts for at least
        half of the abnormal endpoint rows.
        """
        endpoint_counts: Counter[str] = Counter()
        abnormal_rows = 0
        for item in evidence:
            if not can_support_fault(item) or item.entity_type != "path":
                continue
            abnormal = False
            if item.category == "packet_loss_rate":
                if "mtu" in str(item.probe_id or "").lower() or item.source == "ping_test_df_size_sweep":
                    continue
                try:
                    abnormal = float(item.value) >= self.config.abnormal_loss_threshold
                except (TypeError, ValueError):
                    abnormal = False
            elif item.category in {"latency_median", "latency_p95"}:
                abnormal = bool(
                    item.metadata.get("category_anomaly")
                    or item.metadata.get("absolute_anomaly")
                    or item.metadata.get("relative_anomaly")
                    or item.metadata.get("anomaly_type") == "latency_spike"
                )
            # A repeated endpoint-localized anomaly is exactly the signal that
            # distinguishes an access edge from the many ECMP fabric members
            # shared by end-to-end paths. MTU remains on its dedicated
            # expansion path because size thresholds are not endpoint-local.
            if not abnormal:
                continue
            abnormal_rows += 1
            raw_endpoints = (
                item.metadata.get("source") or item.metadata.get("src_name"),
                item.metadata.get("destination") or item.metadata.get("dst_name"),
            )
            if not all(raw_endpoints) and "--" in item.entity_id:
                raw_endpoints = tuple(item.entity_id.split("--", 1))
            for raw in raw_endpoints:
                node = self.graph.resolve_node(str(raw or ""))
                if node and self.graph.roles.get(node) == "client":
                    endpoint_counts[node] += 1
        if abnormal_rows < 2:
            return set()
        suspects = {client for client, count in endpoint_counts.items() if count >= 2 and count / abnormal_rows >= 0.5}
        return {
            link.link_id
            for link in self.graph.links
            if any(endpoint.device in suspects for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b))
        }

    def _concentrated_attachment_access_links(
        self,
        evidence: list[Evidence],
        *,
        allowed_attachments: set[str],
    ) -> set[str]:
        counts: Counter[str] = Counter()
        rows = 0
        for item in evidence:
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                loss_rate = float(item.value)
            except (TypeError, ValueError):
                continue
            if loss_rate < 0.01 or not (
                item.metadata.get("leaf_aggregate") or loss_rate >= self.config.abnormal_loss_threshold
            ):
                continue
            source_attachment = self.graph.resolve_node(attachment_from_metadata(item.metadata, "source"))
            destination_attachment = self.graph.resolve_node(attachment_from_metadata(item.metadata, "destination"))
            if not source_attachment or not destination_attachment:
                continue
            rows += 1
            counts[source_attachment] += 1
            counts[destination_attachment] += 1
        if rows < 3:
            return set()
        suspects = {
            attachment
            for attachment, count in counts.items()
            if attachment in allowed_attachments and count >= 3 and count / rows >= 0.5
        }
        return {
            link.link_id
            for link in self.graph.links
            if any(endpoint.device in suspects for endpoint in (link.physical.endpoint_a, link.physical.endpoint_b))
            and "client"
            in {
                self.graph.roles.get(link.physical.endpoint_a.device),
                self.graph.roles.get(link.physical.endpoint_b.device),
            }
        }

    def _attachments_with_cleared_fabric(self, evidence: list[Evidence]) -> set[str]:
        healthy_links: set[str] = set()
        for item in evidence:
            if (
                item.category != "packet_loss_rate"
                or item.origin is not EvidenceOrigin.ACTIVE_PROBE
                or item.path_observation_confidence < 1.0
            ):
                continue
            try:
                healthy = float(item.value) <= self.config.healthy_loss_threshold
            except (TypeError, ValueError):
                healthy = False
            if healthy:
                healthy_links.update(item.covered_links)
        cleared: set[str] = set()
        for attachment in self.graph.attachment_devices():
            if not self.graph.is_attachment_device(attachment):
                continue
            fabric = {
                link.link_id
                for link in self.graph.network_links()
                if attachment in {link.physical.endpoint_a.device, link.physical.endpoint_b.device}
            }
            if len(fabric) >= 2 and fabric.issubset(healthy_links):
                cleared.add(attachment)
        return cleared

    def _supports_adaptive_expansion(self, evidence: Evidence) -> bool:
        """Reject healthy, missing, and tool-error paths as expansion seeds."""
        if not can_support_fault(evidence):
            return False
        if evidence.category == "packet_loss_rate":
            try:
                return float(evidence.value) >= self.config.abnormal_loss_threshold or bool(
                    evidence.metadata.get("weak_performance_symptom")
                )
            except (TypeError, ValueError):
                return False
        if evidence.category in {"latency_median", "latency_p95"}:
            return bool(
                evidence.metadata.get("category_anomaly")
                or evidence.metadata.get("absolute_anomaly")
                or evidence.metadata.get("relative_anomaly")
            )
        if evidence.category == "packet_size_threshold" and isinstance(evidence.value, dict):
            return bool(evidence.value.get("size_dependent_failure"))
        if evidence.category == "payload_integrity_failure":
            return bool(evidence.value)
        return evidence.category in {"configuration_difference", "interface_counter_delta"}

    def _observes_ranked_family(self, evidence: Evidence) -> bool:
        categories = {
            "packet_loss": {"packet_loss_rate", "payload_integrity_failure"},
            "packet_corruption": {"packet_loss_rate", "payload_integrity_failure"},
            "high_latency": {"latency_median", "latency_p95"},
            "mtu": {"packet_size_threshold", "configuration_difference"},
            "mtu_mismatch": {"packet_size_threshold", "configuration_difference"},
        }
        allowed = categories.get(self.family)
        return allowed is None or evidence.category in allowed

    @staticmethod
    def _expansion_seed_priority(evidence: Evidence) -> tuple[int, float, float, str]:
        """Order bounded expansion by observation strength, never insertion order."""
        origin_priority = {
            EvidenceOrigin.ACTIVE_PROBE: 0,
            EvidenceOrigin.CONFIG_READ: 1,
            EvidenceOrigin.LIVE_TELEMETRY: 2,
            EvidenceOrigin.PUBLIC_OBSERVATION: 3,
            EvidenceOrigin.TOPOLOGY: 4,
            EvidenceOrigin.BASE_CLAIM: 5,
            EvidenceOrigin.UNKNOWN: 6,
        }
        return (
            origin_priority.get(evidence.origin, 6),
            -float(evidence.path_observation_confidence),
            -float(evidence.reliability),
            evidence.evidence_id,
        )

    def _ordered_endpoints(
        self,
        endpoints,
        *,
        causal_endpoint_support,
        symptom_endpoint_support,
        initial_device,
        initial_interface,
    ):
        left, right = endpoints
        left_role = self.graph.roles.get(left.device)
        right_role = self.graph.roles.get(right.device)
        # Access links are diagnosable, but the client-side endpoint is never a
        # network-device localization target.
        if left_role == "client" and right_role != "client":
            return right, left
        if right_role == "client" and left_role != "client":
            return left, right
        left_causal = causal_endpoint_support.get((left.device, left.canonical_interface), 0.0)
        right_causal = causal_endpoint_support.get((right.device, right.canonical_interface), 0.0)
        # Direct causal evidence selects an endpoint before consequence-only
        # counters or an unverified base location.  Submission remains gated
        # by independent evidence, so this ordering does not turn direction
        # alone into a diagnosis.
        if max(left_causal, right_causal) >= 5.0:
            return (right, left) if right_causal > left_causal else (left, right)
        if initial_device == left.device and initial_interface == left.canonical_interface:
            return left, right
        if initial_device == right.device and initial_interface == right.canonical_interface:
            return right, left
        left_symptom = symptom_endpoint_support.get((left.device, left.canonical_interface), 0.0)
        right_symptom = symptom_endpoint_support.get((right.device, right.canonical_interface), 0.0)
        if right_symptom > left_symptom:
            return right, left
        if left_symptom > right_symptom:
            return left, right
        if initial_device == left.device:
            return left, right
        if initial_device == right.device:
            return right, left
        return left, right


__all__ = ["InterfaceRanker"]
