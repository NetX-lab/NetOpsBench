"""Normalize a copied DiagnosisResult and retain an audit trail."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from netopsbench.sdk.agents import DiagnosisResult

from ..models import NormalizedResult
from .fault_type import FaultTypeNormalizer
from .interface import InterfaceNameNormalizer, TopologyIndex


class ResultNormalizer:
    def __init__(self, fault_types: FaultTypeNormalizer):
        self.fault_types = fault_types

    def normalize(
        self,
        result: DiagnosisResult,
        *,
        context: Any | None = None,
        topology: TopologyIndex | None = None,
    ) -> NormalizedResult:
        topology = topology or (
            TopologyIndex.from_context(context) if context is not None else TopologyIndex(devices={})
        )
        findings = dict(result.findings or {})
        location_raw = findings.get("location")
        location = dict(location_raw) if isinstance(location_raw, Mapping) else {}

        fault_type = self.fault_types.normalize(findings.get("fault_type"))
        original_device = location.get("device")
        device = topology.resolve_device(original_device) or original_device
        interface = InterfaceNameNormalizer(topology).normalize(device, location.get("interface"))

        findings["fault_type"] = fault_type.value
        findings["location"] = {"device": device, "interface": interface.value}

        errors = tuple(
            error for error in (fault_type.validation_error, interface.validation_error) if error is not None
        )
        metadata = dict(result.metadata or {})
        harness_metadata = dict(metadata.get("diagnostic_harness") or {})
        harness_metadata["normalization"] = {
            "fault_type": {
                "original": fault_type.original,
                "value": fault_type.value,
                "normalized_from": fault_type.normalized_from,
                "is_canonical": fault_type.is_canonical,
            },
            "interface": {
                "device": interface.device,
                "original": interface.original,
                "value": interface.value,
                "link_id": interface.link_id,
            },
            "topology_source": topology.source,
            "validation_errors": list(errors),
        }
        metadata["diagnostic_harness"] = harness_metadata
        normalized = replace(result, findings=findings, metadata=metadata)
        return NormalizedResult(
            result=normalized,
            fault_type=fault_type,
            interface=interface,
            validation_errors=errors,
        )


__all__ = ["ResultNormalizer"]
