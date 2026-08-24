from examples.agents.diagnostic_harness.normalization.interface import InterfaceNameNormalizer

from .diagnostic_harness_helpers import sample_topology


def test_linux_and_vendor_aliases_map_to_evaluator_interface():
    normalizer = InterfaceNameNormalizer(sample_topology())

    for alias in ("eth2", "e1-2", "ethernet-1/2", "Ethernet4"):
        result = normalizer.normalize("leaf5", alias)
        assert result.value == "Ethernet4"
        assert result.validation_error is None


def test_interface_is_never_resolved_on_the_wrong_device():
    result = InterfaceNameNormalizer(sample_topology()).normalize("leaf1", "eth2")

    assert result.value == "eth2"
    assert "does not belong" in result.validation_error
