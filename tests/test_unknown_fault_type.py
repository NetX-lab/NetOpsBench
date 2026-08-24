from examples.agents.diagnostic_harness.normalization.fault_type import FaultTypeNormalizer


def test_unknown_fault_type_is_preserved_and_marked_invalid():
    normalized = FaultTypeNormalizer().normalize("mysterious_fabric_impairment")

    assert normalized.value == "mysterious_fabric_impairment"
    assert normalized.original == "mysterious_fabric_impairment"
    assert not normalized.is_canonical
    assert "unknown fault type" in normalized.validation_error
