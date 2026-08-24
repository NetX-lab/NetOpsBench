"""Multi-sample RTT matrix using the real ping toolkit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median, pstdev

from ..config import LatencyProbeConfig
from ..models import (
    Evidence,
    EvidenceDirection,
    EvidenceOrigin,
    ProbeOutcome,
    ProbePair,
    RankedInterfaceCandidate,
)
from .base import ProbeBudget, invoke_link_ping, invoke_ping, invoke_tool, percentile_nearest_rank, tool_error_evidence


@dataclass(frozen=True)
class RTTProbeResult:
    source: str
    destination: str
    sample_count: int
    median_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    jitter_ms: float
    packet_loss_rate: float
    absolute_anomaly: bool
    relative_anomaly: bool
    control: bool = False
    selection: str | None = None
    reference_median_ms: float | None = None
    reference_p95_ms: float | None = None
    median_multiplier: float | None = None
    p95_multiplier: float | None = None


class RTTMatrixProbe:
    def __init__(self, config: LatencyProbeConfig | None = None):
        self.config = config or LatencyProbeConfig()

    async def run(
        self,
        context,
        *,
        pairs: list[ProbePair],
        budget: ProbeBudget,
        probe_id: str = "rtt-matrix",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        evidence: list[Evidence] = []
        measured: list[tuple[ProbePair, tuple[float, ...], float]] = []

        for pair_index, pair in enumerate(pairs[: self.config.max_pairs], start=1):
            try:
                budget.reserve_probe()
            except RuntimeError as exc:
                evidence.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-budget-{pair_index}",
                        probe_id=probe_id,
                        entity_id=f"{pair.source}--{pair.destination}",
                        error=str(exc),
                        source="harness_budget",
                    )
                )
                break
            if pair.metadata.get("link_probe"):
                observation, error = await invoke_link_ping(
                    context,
                    source=pair.source,
                    target_device=str(pair.metadata["target_device"]),
                    source_interface=str(pair.metadata["source_interface"]),
                    target_interface=str(pair.metadata["target_interface"]),
                    count=self.config.samples_per_pair,
                    budget=budget,
                    timeout_seconds=self.config.timeout_seconds,
                )
                probe_source = "ping_link_test"
            else:
                observation, error = await invoke_ping(
                    context,
                    source=pair.source,
                    destination=pair.destination,
                    count=self.config.samples_per_pair,
                    budget=budget,
                    timeout_seconds=self.config.timeout_seconds,
                    source_interface=pair.metadata.get("source_interface"),
                )
                probe_source = "ping_test"
            if observation is None:
                evidence.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-error-{pair_index}",
                        probe_id=probe_id,
                        entity_id=f"{pair.source}--{pair.destination}",
                        error=error or "unknown ping failure",
                        source=probe_source,
                    )
                )
                if error and "budget exhausted" in error:
                    break
                continue
            if not observation.rtt_samples_ms:
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-missing-rtt-{pair_index}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="missing_observation",
                        value={"reason": "ping output contained no per-packet RTT samples"},
                        source=probe_source,
                        timestamp=datetime.now(UTC),
                        reliability=0.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        supports_submission=False,
                    )
                )
                continue
            measured.append((pair, observation.rtt_samples_ms, observation.loss_rate))

        control_medians = [median(samples) for pair, samples, _loss in measured if bool(pair.metadata.get("control"))]
        control_p95s = [
            percentile_nearest_rank(samples, 0.95)
            for pair, samples, _loss in measured
            if bool(pair.metadata.get("control"))
        ]
        comparison_median = median(control_medians) if control_medians else None
        comparison_p95 = median([value for value in control_p95s if value is not None]) if control_p95s else None
        # Exact one-hop probes across an ECMP cut form their own same-role
        # control cohort.  On a large fabric the original anomaly/control pair
        # may hash onto a healthy member; comparing every member with the
        # lower half of its peers recovers a relative baseline without a
        # topology-wide historical database.  A uniform slow cohort remains
        # unflagged because no member separates from its peers.
        link_rows = [(samples, pair) for pair, samples, _loss in measured if pair.metadata.get("link_probe")]
        if comparison_median is None and len(link_rows) >= 3:
            link_medians = sorted(float(median(samples)) for samples, _pair in link_rows)
            link_p95s = sorted(
                float(percentile_nearest_rank(samples, 0.95) or median(samples)) for samples, _ in link_rows
            )
            cohort_size = max(1, len(link_medians) // 2)
            comparison_median = float(median(link_medians[:cohort_size]))
            comparison_p95 = float(median(link_p95s[:cohort_size]))
        results: list[RTTProbeResult] = []
        for pair_index, (pair, samples, loss_rate) in enumerate(measured, start=1):
            median_ms = float(median(samples))
            p95_ms = float(percentile_nearest_rank(samples, 0.95) or median_ms)
            control = bool(pair.metadata.get("control"))
            baseline = _positive_float(pair.metadata.get("baseline"))
            reference_median = baseline or comparison_median
            reference_p95 = comparison_p95 or reference_median
            median_multiplier = median_ms / reference_median if reference_median else None
            p95_multiplier = p95_ms / reference_p95 if reference_p95 else None
            median_absolute_anomaly = median_ms >= self.config.absolute_threshold_ms
            p95_absolute_anomaly = p95_ms >= self.config.absolute_threshold_ms
            median_relative_anomaly = bool(
                not control
                and median_multiplier is not None
                and median_multiplier >= self.config.relative_multiplier_threshold
            )
            p95_relative_anomaly = bool(
                not control
                and p95_multiplier is not None
                and p95_multiplier >= self.config.relative_multiplier_threshold
            )
            absolute_anomaly = median_absolute_anomaly or p95_absolute_anomaly
            relative_anomaly = median_relative_anomaly or p95_relative_anomaly
            result = RTTProbeResult(
                source=pair.source,
                destination=pair.destination,
                sample_count=len(samples),
                median_ms=median_ms,
                p95_ms=p95_ms,
                min_ms=min(samples),
                max_ms=max(samples),
                jitter_ms=float(pstdev(samples)) if len(samples) > 1 else 0.0,
                packet_loss_rate=loss_rate,
                absolute_anomaly=absolute_anomaly,
                relative_anomaly=relative_anomaly,
                control=control,
                selection=str(pair.metadata.get("selection") or "") or None,
                reference_median_ms=reference_median,
                reference_p95_ms=reference_p95,
                median_multiplier=median_multiplier,
                p95_multiplier=p95_multiplier,
            )
            results.append(result)
            reliability = min(1.0, len(samples) / self.config.samples_per_pair)
            common_metadata = {
                "source": pair.source,
                "destination": pair.destination,
                "sample_count": len(samples),
                "min_ms": result.min_ms,
                "max_ms": result.max_ms,
                "jitter_ms": result.jitter_ms,
                "packet_loss_rate": loss_rate,
                "control": control,
                "control_median_ms": comparison_median,
                "control_p95_ms": comparison_p95,
                "reference_median_ms": reference_median,
                "reference_p95_ms": reference_p95,
                "median_multiplier": median_multiplier,
                "p95_multiplier": p95_multiplier,
                "absolute_anomaly": absolute_anomaly,
                "relative_anomaly": relative_anomaly,
                # Keep category-specific flags separate. A p95-only relative
                # jitter spike must not turn the median/path into a direct
                # high-latency link candidate; a stable 120 ms median still
                # remains directly abnormal.
                "median_absolute_anomaly": median_absolute_anomaly,
                "p95_absolute_anomaly": p95_absolute_anomaly,
                "median_relative_anomaly": median_relative_anomaly,
                "p95_relative_anomaly": p95_relative_anomaly,
                "interface_direct_anomaly": median_absolute_anomaly or median_relative_anomaly or p95_absolute_anomaly,
                "source_leaf": pair.source_leaf,
                "destination_leaf": pair.destination_leaf,
                "source_attachment": pair.source_attachment,
                "destination_attachment": pair.destination_attachment,
                "selection": pair.metadata.get("selection"),
                "suspect_attachment": pair.metadata.get("suspect_attachment"),
                "suspect_leaf": pair.metadata.get("suspect_leaf"),
                "baseline": baseline,
                "threshold": pair.metadata.get("threshold"),
                "link_probe": bool(pair.metadata.get("link_probe")),
                "source_interface": pair.metadata.get("source_interface"),
                "target_interface": pair.metadata.get("target_interface"),
            }
            median_metadata = {
                **common_metadata,
                "category_anomaly": median_absolute_anomaly or median_relative_anomaly,
                "absolute_anomaly": median_absolute_anomaly,
                "relative_anomaly": median_relative_anomaly,
            }
            p95_metadata = {
                **common_metadata,
                "category_anomaly": p95_absolute_anomaly,
                "absolute_anomaly": p95_absolute_anomaly,
                "relative_anomaly": p95_relative_anomaly,
            }
            # ICMP ping measures round-trip latency. Reversing the endpoints
            # still traverses both sides of the same physical link and cannot
            # identify which endpoint introduced delay. Keep this as direct
            # link evidence; endpoint binding requires an asymmetric one-way
            # observation or endpoint-specific queue/config telemetry.
            evidence.extend(
                (
                    Evidence(
                        evidence_id=f"{probe_id}-median-{pair_index}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="latency_median",
                        value=median_ms,
                        source="ping_test_rtt_matrix",
                        timestamp=datetime.now(UTC),
                        reliability=reliability,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        metadata=median_metadata,
                    ),
                    Evidence(
                        evidence_id=f"{probe_id}-p95-{pair_index}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="latency_p95",
                        value=p95_ms,
                        source="ping_test_rtt_matrix",
                        timestamp=datetime.now(UTC),
                        reliability=reliability,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        metadata=p95_metadata,
                    ),
                    Evidence(
                        evidence_id=f"{probe_id}-loss-{pair_index}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="packet_loss_rate",
                        value=loss_rate,
                        source="ping_test_rtt_matrix",
                        timestamp=datetime.now(UTC),
                        reliability=reliability,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        metadata=common_metadata,
                    ),
                )
            )

        delta_calls = budget.tool_calls - starting["tool_calls"]
        delta_packets = budget.probe_packets - starting["probe_packets"]
        errors = [item for item in evidence if item.category in {"tool_error", "missing_observation"}]
        status = "completed" if results and not errors else "partial" if results else "failed"
        return ProbeOutcome(
            probe_id=probe_id,
            status=status,
            evidence=tuple(evidence),
            observations=tuple(results),
            tool_calls=delta_calls,
            probe_packets=delta_packets,
            error=str(errors[0].value) if not results and errors else None,
            metadata={
                "pairs_completed": len(results),
                "control_median_ms": comparison_median,
                "control_p95_ms": comparison_p95,
                "anomaly_pairs": sum(item.selection == "anomaly" for item in results),
                "control_pairs": sum(item.control for item in results),
                "link_isolation_pairs": sum(item.selection == "link_isolation" for item in results),
            },
        )


class DirectionalLinkLatencyProbe:
    """Bind a latency anomaly to one link endpoint using one-way captures."""

    def __init__(self, config: LatencyProbeConfig | None = None):
        self.config = config or LatencyProbeConfig()

    async def run(
        self,
        context,
        *,
        candidate: RankedInterfaceCandidate,
        budget: ProbeBudget,
        probe_id: str = "directional-link-latency",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        try:
            budget.reserve_probe()
        except RuntimeError as exc:
            return ProbeOutcome(
                probe_id=probe_id,
                status="failed",
                evidence=(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-budget",
                        probe_id=probe_id,
                        entity_id=candidate.link_id,
                        error=str(exc),
                        source="harness_budget",
                    ),
                ),
                error=str(exc),
            )

        count = max(3, min(int(self.config.samples_per_pair), 20))
        invocation = await invoke_tool(
            context,
            "latency_link_test",
            {
                "device_a": candidate.primary_device,
                "interface_a": candidate.primary_interface,
                "device_b": candidate.peer_device,
                "interface_b": candidate.peer_interface,
                "count": count,
            },
            budget=budget,
            timeout_seconds=self.config.timeout_seconds,
            packets=count * 2,
        )
        if not invocation.success or invocation.data is None:
            error = invocation.error or "one-way latency tool failed"
            return ProbeOutcome(
                probe_id=probe_id,
                status="failed",
                evidence=(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-error",
                        probe_id=probe_id,
                        entity_id=candidate.link_id,
                        error=error,
                        source="latency_link_test",
                    ),
                ),
                tool_calls=budget.tool_calls - starting["tool_calls"],
                probe_packets=budget.probe_packets - starting["probe_packets"],
                error=error,
            )

        directions = [
            item
            for item in invocation.data.get("directions", ())
            if isinstance(item, Mapping) and int(item.get("sample_count") or 0) > 0
        ]
        if len(directions) != 2:
            reason = "one-way latency tool did not return two complete directions"
            return ProbeOutcome(
                probe_id=probe_id,
                status="partial",
                evidence=(
                    Evidence(
                        evidence_id=f"{probe_id}-missing",
                        entity_type="path",
                        entity_id=candidate.link_id,
                        category="missing_observation",
                        value={"reason": reason},
                        source="latency_link_test",
                        timestamp=datetime.now(UTC),
                        reliability=0.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        supports_submission=False,
                    ),
                ),
                tool_calls=budget.tool_calls - starting["tool_calls"],
                probe_packets=budget.probe_packets - starting["probe_packets"],
                error=reason,
            )

        medians = [float(item.get("median_ms") or 0.0) for item in directions]
        high_index = max(range(2), key=medians.__getitem__)
        low_index = 1 - high_index
        low_reference = max(medians[low_index], 0.001)
        endpoint_discriminated = bool(
            medians[high_index] >= self.config.absolute_threshold_ms
            and medians[high_index] / low_reference >= self.config.relative_multiplier_threshold
        )
        fault_source = str(directions[high_index].get("source") or "") if endpoint_discriminated else ""
        fault_interface = str(directions[high_index].get("source_interface") or "") if endpoint_discriminated else ""
        evidence: list[Evidence] = []
        for index, item in enumerate(directions, start=1):
            source = str(item.get("source") or "")
            target = str(item.get("target_device") or "")
            direction = (
                EvidenceDirection.A_TO_B
                if source == candidate.primary_device
                else EvidenceDirection.B_TO_A
                if source == candidate.peer_device
                else EvidenceDirection.UNKNOWN
            )
            sample_count = int(item.get("sample_count") or 0)
            reliability = min(1.0, sample_count / count)
            median_ms = float(item.get("median_ms") or 0.0)
            p95_ms = float(item.get("p95_ms") or median_ms)
            is_fault_direction = endpoint_discriminated and source == fault_source
            metadata = {
                "source": source,
                "destination": target,
                "source_interface": item.get("source_interface"),
                "target_interface": item.get("target_interface"),
                "sample_count": sample_count,
                "selection": "directional_link_latency",
                "link_probe": True,
                "one_way": True,
                "absolute_anomaly": median_ms >= self.config.absolute_threshold_ms,
                "relative_anomaly": is_fault_direction,
                "category_anomaly": is_fault_direction,
                "interface_direct_anomaly": is_fault_direction,
            }
            if is_fault_direction:
                metadata.update(
                    fault_endpoint_device=fault_source,
                    fault_endpoint_interface=fault_interface,
                )
            common = {
                "entity_type": "path",
                "entity_id": f"{source}--{target}",
                "source": "latency_link_test",
                "timestamp": datetime.now(UTC),
                "reliability": reliability,
                "probe_id": probe_id,
                "origin": EvidenceOrigin.ACTIVE_PROBE,
                "independence_key": f"probe:{probe_id}:direction:{index}",
                "direction": direction,
                "metadata": metadata,
                "observed_path": (candidate.link_id,),
                "possible_paths": ((candidate.link_id,),),
                "covered_links": (candidate.link_id,),
                "path_observation_confidence": 1.0,
            }
            evidence.extend(
                (
                    Evidence(
                        evidence_id=f"{probe_id}-median-{index}",
                        category="latency_median",
                        value=median_ms,
                        **common,
                    ),
                    Evidence(
                        evidence_id=f"{probe_id}-p95-{index}",
                        category="latency_p95",
                        value=p95_ms,
                        **common,
                    ),
                )
            )

        return ProbeOutcome(
            probe_id=probe_id,
            status="completed",
            evidence=tuple(evidence),
            observations=tuple(directions),
            tool_calls=budget.tool_calls - starting["tool_calls"],
            probe_packets=budget.probe_packets - starting["probe_packets"],
            metadata={
                "endpoint_discriminated": endpoint_discriminated,
                "fault_endpoint_device": fault_source or None,
                "fault_endpoint_interface": fault_interface or None,
            },
        )


def _positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = ["DirectionalLinkLatencyProbe", "RTTMatrixProbe", "RTTProbeResult"]
