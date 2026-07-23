from netopsbench.platform.observability.validation import check_observability, extract_interface_names


def test_extract_interface_names_ignores_empty_counter_identity_samples():
    csv_text = "\n".join(
        [
            ",result,table,_time,_field,_value,name,path,source",
            ",,0,2026-07-01T18:49:04Z,Ethernet100,,Ethernet100,/COUNTERS/Ethernet100,leaf1",
            ",,0,2026-07-01T18:49:04Z,in_octets,42,Ethernet0,/COUNTERS/Ethernet0,leaf1",
            ",,0,2026-07-01T18:49:04Z,out_octets,43,Ethernet4,/COUNTERS/Ethernet4,leaf1",
        ]
    )

    assert extract_interface_names(csv_text) == ["Ethernet0", "Ethernet4"]


def test_interface_observability_queries_are_scoped_by_topology_id():
    queries: list[str] = []

    def query_runner(query: str) -> str:
        queries.append(query)
        if '_measurement == "pingmesh"' in query:
            return ",result,table,_time,_value\n,,0,2026-07-01T18:49:04Z,1\n"
        if '_measurement == "interfaces"' in query:
            return "\n".join(
                [
                    ",result,table,_time,_field,_value,name,path,source,topology_id",
                    ",,0,2026-07-01T18:49:04Z,in_octets,42,Ethernet0,/COUNTERS/Ethernet0,leaf1,lab-a",
                ]
            )
        return ",result,table,_time,_value\n"

    errors = check_observability(
        query_runner,
        bucket="netopsbench",
        obs_device="leaf1",
        topology_id="lab-a",
        active_interfaces=["Ethernet0"],
    )

    assert errors == []
    interface_queries = [query for query in queries if '_measurement == "interfaces"' in query]
    assert len(interface_queries) == 1
    assert all('r.topology_id == "lab-a"' in query for query in interface_queries)
    assert 'group(columns: ["name", "path", "_field"])' in interface_queries[0]
    assert "|> last()" in interface_queries[0]
    assert "limit(n: 2000)" not in interface_queries[0]


def test_observability_existence_queries_are_globally_bounded():
    queries: list[str] = []

    def query_runner(query: str) -> str:
        queries.append(query)
        if '_measurement == "interfaces"' in query:
            return ",result,table,_time,_field,_value,name,path\n,,0,2026-07-01T18:49:04Z,in_octets,1,Ethernet0,/COUNTERS/Ethernet0\n"
        return ",result,table,_time,_value\n,,0,2026-07-01T18:49:04Z,1\n"

    errors = check_observability(
        query_runner,
        bucket="netopsbench",
        obs_device="leaf1",
        bgp_device="spine1",
        topology_id="lab-a",
        syslog_marker="health-marker",
        active_interfaces=["Ethernet0"],
    )

    assert errors == []
    assert len(queries) == 4
    pingmesh_query = next(query for query in queries if '_measurement == "pingmesh"' in query)
    bgp_query = next(query for query in queries if '_measurement == "bgp_event_index"' in query)
    syslog_query = next(query for query in queries if '_measurement == "syslog"' in query)
    assert '_field == "rtt_p99"' in pingmesh_query
    assert "|> group()" in pingmesh_query
    assert "|> limit(n: 1)" in pingmesh_query
    assert "|> group()" in bgp_query
    assert '_field == "schema_version"' in bgp_query
    assert 'r.topology_id == "lab-a"' in syslog_query
    assert "|> limit(n: 1)" in syslog_query
