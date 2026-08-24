"""Final result validation independent from benchmark ground truth."""

from __future__ import annotations

from collections.abc import Mapping

from netopsbench.agents.base import VALID_AGENT_VERDICTS
from netopsbench.evaluator.fault_type_judge import supported_fault_types
from netopsbench.sdk.agents import DiagnosisResult

from ..config import NormalizationConfig
from ..models import ValidationIssue, ValidationReport
from ..normalization.interface import TopologyIndex


class FinalResultValidator:
    def __init__(self, normalization: NormalizationConfig | None = None):
        self.config = normalization or NormalizationConfig()
        self.canonical_fault_types = frozenset(supported_fault_types())

    def validate(self, result: DiagnosisResult, *, topology: TopologyIndex) -> ValidationReport:
        issues: list[ValidationIssue] = []
        findings = result.findings if isinstance(result.findings, Mapping) else {}
        location_raw = findings.get("location")
        location = location_raw if isinstance(location_raw, Mapping) else {}
        fault_type = findings.get("fault_type")
        device = location.get("device")
        interface = location.get("interface")

        if result.verdict not in VALID_AGENT_VERDICTS:
            issues.append(ValidationIssue("invalid_verdict", f"invalid verdict: {result.verdict}", "verdict"))
        if not isinstance(result.confidence, (int, float)) or not 0.0 <= float(result.confidence) <= 1.0:
            issues.append(ValidationIssue("invalid_confidence", "confidence must be between 0 and 1", "confidence"))

        if device and topology.resolve_device(str(device)) is None:
            issues.append(ValidationIssue("unknown_device", f"device is not in topology: {device}", "location.device"))
        if interface:
            if not device:
                issues.append(
                    ValidationIssue(
                        "interface_without_device",
                        "an interface location requires a device",
                        "location.interface",
                    )
                )
            elif not topology.interface_mapping_available:
                issues.append(
                    ValidationIssue(
                        "interface_topology_unavailable",
                        "canonical topology interface mapping is unavailable",
                        "location.interface",
                    )
                )
            elif topology.resolve_interface(str(device), str(interface)) is None:
                issues.append(
                    ValidationIssue(
                        "interface_not_on_device",
                        f"interface {interface} does not belong to {device}",
                        "location.interface",
                    )
                )

        if result.verdict == "network_healthy" and any(value for value in (fault_type, device, interface)):
            issues.append(
                ValidationIssue(
                    "healthy_with_fault_location",
                    "network_healthy cannot carry a fault type or location",
                    "findings",
                )
            )

        if result.verdict == "fault_detected":
            if not fault_type:
                issues.append(ValidationIssue("missing_fault_type", "fault_detected requires fault_type", "fault_type"))
            elif fault_type not in self.canonical_fault_types:
                issues.append(
                    ValidationIssue(
                        "unknown_fault_type",
                        f"fault type is not canonical: {fault_type}",
                        "fault_type",
                    )
                )
            if not device:
                issues.append(ValidationIssue("missing_device", "fault_detected requires a device", "location.device"))
            if fault_type in self.config.interface_required_fault_types and not interface:
                issues.append(
                    ValidationIssue(
                        "missing_interface",
                        f"{fault_type} requires an interface location",
                        "location.interface",
                    )
                )

        return ValidationReport(tuple(issues))


__all__ = ["FinalResultValidator"]
