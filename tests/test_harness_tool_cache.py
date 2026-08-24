from examples.agents.diagnostic_harness.evidence.cache import TTLToolCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_cache_normalizes_parameter_order_and_expires_by_ttl():
    clock = Clock()
    cache = TTLToolCache(clock=clock)
    first = cache.key("get_device_interfaces", device="leaf1", parameters={"b": 2, "a": 1})
    reordered = cache.key("get_device_interfaces", device="leaf1", parameters={"a": 1, "b": 2})

    assert first == reordered
    cache.set(first, {"up": True}, ttl_seconds=10)
    assert cache.get(reordered) == {"up": True}

    clock.now += 10
    assert cache.get(first) is None
    assert cache.stats == {"hits": 1, "misses": 1, "size": 0}


def test_active_probe_ttl_zero_is_not_reused():
    cache = TTLToolCache()
    key = cache.key("ping_test", parameters={"count": 20, "payload_size": 1472})

    cache.set(key, {"loss": 0.0}, ttl_seconds=0)

    assert not cache.has(key)
    assert cache.get(key) is None


def test_interface_is_part_of_cache_identity():
    cache = TTLToolCache()

    left = cache.key("get_interface_metrics", device="leaf1", interface="Ethernet0")
    right = cache.key("get_interface_metrics", device="leaf1", interface="Ethernet4")

    assert left != right
