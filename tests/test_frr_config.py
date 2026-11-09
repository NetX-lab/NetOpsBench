"""One BGP config parser serves generated artifacts and live fault snapshots."""

from types import SimpleNamespace

from netopsbench.platform.faults.services.routing_runtime import RoutingRuntime
from netopsbench.platform.utils.frr import parse_bgp_config

CONFIG = """Building configuration...
Current configuration:
!
router bgp 65001
 neighbor 10.0.0.2 route-map IMPORT in
 neighbor 10.0.0.2 remote-as 65002
 neighbor 10.0.0.2 password a-secret
 neighbor 10.0.0.2 update-source Ethernet0
 neighbor 10.0.0.2 route-map EXPORT out
 address-family ipv4 unicast
  neighbor 10.0.0.2 route-map IMPORT in
  network 192.168.1.0/30 route-map ORIGIN
  network 192.168.1.0/30 route-map ORIGIN
  network 192.168.2.0/30
 exit-address-family
!
"""


def test_parser_preserves_neighbor_options_and_deduplicates_policies_and_networks():
    parsed = parse_bgp_config(CONFIG)
    assert parsed == {
        "local_as": 65001,
        "neighbors": [
            {
                "peer_ip": "10.0.0.2",
                "remote_as": 65002,
                "password": "a-secret",
                "update_source": "Ethernet0",
                "route_maps": [{"name": "IMPORT", "direction": "in"}, {"name": "EXPORT", "direction": "out"}],
            }
        ],
        "networks": [
            {"prefix": "192.168.1.0/30", "route_map": "ORIGIN"},
            {"prefix": "192.168.2.0/30", "route_map": None},
        ],
    }


def test_live_snapshots_share_parser_and_require_the_requested_target():
    response = SimpleNamespace(returncode=0, stdout=CONFIG)
    runtime = RoutingRuntime(SimpleNamespace(vtysh=lambda *args: response), SimpleNamespace())
    assert runtime.get_bgp_config_snapshot("leaf1") == parse_bgp_config(CONFIG)
    assert runtime.pick_bgp_neighbor("leaf1", "10.0.0.2")["remote_as"] == 65002
    assert runtime.pick_bgp_neighbor("leaf1", "10.0.0.99") is None
    assert runtime.pick_advertised_network("leaf1", "192.168.1.0/30")["route_map"] == "ORIGIN"
    assert runtime.pick_advertised_network("leaf1", "192.168.99.0/30") is None
    response.returncode = 1
    assert runtime.get_bgp_config_snapshot("leaf1") == {"local_as": None, "neighbors": [], "networks": []}
