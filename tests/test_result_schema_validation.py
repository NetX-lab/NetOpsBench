import pytest

from examples.agents.diagnostic_harness.config import NormalizationConfig
from examples.agents.diagnostic_harness.verification.schema_validator import FinalResultValidator

from .diagnostic_harness_helpers import diagnosis_result, sample_topology


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (
            {"verdict": "network_healthy", "fault_type": "packet_loss", "device": "leaf1", "interface": None},
            "healthy_with_fault_location",
        ),
        (
            {"verdict": "fault_detected", "fault_type": "packet_loss", "device": "leaf1", "interface": None},
            "missing_interface",
        ),
        (
            {"verdict": "fault_detected", "fault_type": "not_canonical", "device": "leaf1", "interface": None},
            "unknown_fault_type",
        ),
        (
            {"verdict": "fault_detected", "fault_type": "acl_block", "device": "leaf1", "interface": "Ethernet0"},
            "unknown_fault_type",
        ),
        (
            {"verdict": "fault_detected", "fault_type": "device_down", "device": "missing", "interface": None},
            "unknown_device",
        ),
        (
            {"verdict": "fault", "fault_type": "device_down", "device": "leaf1", "interface": None},
            "invalid_verdict",
        ),
        (
            {
                "verdict": "fault_detected",
                "fault_type": "link_flapping",
                "device": "leaf1",
                "interface": "eth2",
            },
            "interface_not_on_device",
        ),
    ],
)
def test_invalid_results_have_structured_errors(payload, error_code):
    report = FinalResultValidator(NormalizationConfig()).validate(diagnosis_result(payload), topology=sample_topology())

    assert error_code in {issue.code for issue in report.issues}


def test_valid_fault_and_healthy_results_pass():
    validator = FinalResultValidator()

    fault = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_flapping",
            "device": "leaf5",
            "interface": "Ethernet4",
            "confidence": 0.9,
        }
    )
    healthy = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.9,
        }
    )

    assert validator.validate(fault, topology=sample_topology()).valid
    assert validator.validate(healthy, topology=sample_topology()).valid


def test_confidence_must_be_in_range():
    payload = {
        "verdict": "fault_detected",
        "fault_type": "device_down",
        "device": "leaf1",
        "interface": None,
        "confidence": 1.1,
    }

    report = FinalResultValidator().validate(diagnosis_result(payload), topology=sample_topology())

    assert "invalid_confidence" in {issue.code for issue in report.issues}
