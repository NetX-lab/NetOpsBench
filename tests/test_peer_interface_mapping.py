from .diagnostic_harness_helpers import sample_topology


def test_peer_interface_is_resolved_from_physical_link():
    topology = sample_topology()

    peer = topology.peer("leaf5", "eth2")

    assert peer is not None
    assert peer.device == "spine3"
    assert peer.canonical_interface == "Ethernet4"
    assert topology.physical_link("spine3", "e1-2").link_id == "leaf5:Ethernet4--spine3:Ethernet4"


def test_unknown_interface_has_no_guessed_peer():
    assert sample_topology().peer("leaf5", "eth99") is None
