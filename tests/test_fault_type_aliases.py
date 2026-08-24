from examples.agents.diagnostic_harness.normalization.fault_type import FaultTypeNormalizer
from examples.agents.diagnostic_harness.normalization.result import ResultNormalizer

from .diagnostic_harness_helpers import diagnosis_result, sample_topology


def test_known_fault_type_aliases_are_canonicalized():
    normalizer = FaultTypeNormalizer()

    assert normalizer.normalize("link_flap").value == "link_flapping"
    assert normalizer.normalize("interface_flapping").value == "link_flapping"
    assert normalizer.normalize("routing_policy_error").value == "route_policy_misconfig"
    assert normalizer.normalize("missing_bgp_network_statement").value == "route_policy_misconfig"
    assert normalizer.normalize("mtu_misconfig").value == "mtu_mismatch"
    assert normalizer.normalize("mtu_misconfiguration").value == "mtu_mismatch"
    assert normalizer.normalize("acl_deny").value == "acl_misconfig"
    acl = normalizer.normalize("acl_block")
    assert acl.value == "acl_misconfig"
    assert acl.normalized_from == "acl_block"


def test_alias_map_is_configurable():
    normalized = FaultTypeNormalizer({"custom_policy_name": "route_policy_misconfig"}).normalize("custom policy name")

    assert normalized.value == "route_policy_misconfig"
    assert normalized.normalized_from == "custom policy name"


def test_unknown_acl_like_label_is_not_silently_mapped():
    normalized = FaultTypeNormalizer().normalize("acl_probably_broken")

    assert normalized.value == "acl_probably_broken"
    assert not normalized.is_canonical
    assert normalized.validation_error == "unknown fault type: acl_probably_broken"


def test_canonical_acl_label_is_not_rewritten():
    normalized = FaultTypeNormalizer().normalize("acl_misconfig")

    assert normalized.value == "acl_misconfig"
    assert normalized.normalized_from is None


def test_acl_alias_normalization_preserves_location_and_does_not_touch_healthy():
    topology = sample_topology()
    normalizer = ResultNormalizer(FaultTypeNormalizer())
    acl = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "acl_block",
            "device": "leaf5",
            "interface": "Ethernet4",
            "confidence": 0.95,
            "evidence": ["An active ACL DROP rule is bound to the interface."],
        }
    )
    healthy = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.95,
        }
    )

    acl_result = normalizer.normalize(acl, topology=topology).result
    healthy_result = normalizer.normalize(healthy, topology=topology).result

    assert acl_result.findings["fault_type"] == "acl_misconfig"
    assert acl_result.findings["location"] == {"device": "leaf5", "interface": "Ethernet4"}
    assert healthy_result.findings == healthy.findings
