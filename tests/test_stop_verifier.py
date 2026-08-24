from examples.agents.diagnostic_harness.config import NormalizationConfig
from examples.agents.diagnostic_harness.models import DiagnosabilityDecision
from examples.agents.diagnostic_harness.verification import FinalResultValidator, StopVerifier

from .diagnostic_harness_helpers import diagnosis_result, sample_topology


def test_stop_verifier_requires_both_gate_and_schema():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": "leaf5",
            "interface": "Ethernet4",
            "confidence": 0.9,
        }
    )
    verifier = StopVerifier(FinalResultValidator(NormalizationConfig()))

    accepted = verifier.verify(
        result,
        decision=DiagnosabilityDecision(True, "ready"),
        topology=sample_topology(),
    )
    withheld = verifier.verify(
        result,
        decision=DiagnosabilityDecision(False, "missing evidence"),
        topology=sample_topology(),
    )

    assert accepted.can_submit
    assert not withheld.can_submit
