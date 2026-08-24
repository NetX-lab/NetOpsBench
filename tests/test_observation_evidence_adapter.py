from types import SimpleNamespace

from examples.agents.diagnostic_harness.evidence import evidence_from_public_observations


def test_public_pingmesh_anomalies_become_typed_evidence_without_private_metadata():
    context = SimpleNamespace(
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "query_status": {"ok": True, "error": None},
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client9",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf5",
                            "value": 12.5,
                            "sample_count": 30,
                            "persistence": "persistent",
                        },
                        {
                            "type": "mtu_or_fragmentation_suspect",
                            "src_name": "client2",
                            "dst_name": "client10",
                            "value": 100.0,
                            "sample_count": 9,
                            "persistence": "persistent",
                        },
                        {
                            "type": "latency_spike",
                            "src_name": "client3",
                            "dst_name": "client15",
                            "value": 40.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        },
                    ],
                }
            }
        }
    )

    evidence = evidence_from_public_observations(context)

    assert [item.category for item in evidence] == [
        "packet_loss_rate",
        "packet_size_threshold",
        "latency_p95",
    ]
    assert evidence[0].value == 0.125
    assert evidence[1].value["threshold_payload_size"] is None
    assert evidence[1].usable_for_planning
    assert not evidence[1].supports_submission
    assert evidence[1].metadata["planning_only"]
    assert evidence[0].metadata["src_attachment"] == "leaf1"
    assert evidence[0].metadata["dst_attachment"] == "leaf5"
    assert all("fault_type" not in item.metadata for item in evidence)
    assert all("target_device" not in item.metadata for item in evidence)


def test_pingmesh_query_error_is_non_supporting_tool_error():
    context = SimpleNamespace(
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "query_status": {"ok": False, "error": "query timed out"},
                    "anomalies": [],
                }
            }
        }
    )

    evidence = evidence_from_public_observations(context)

    assert len(evidence) == 1
    assert evidence[0].category == "tool_error"
    assert evidence[0].reliability == 0


def test_pingmesh_rows_from_one_window_share_one_independence_key():
    context = SimpleNamespace(
        symptoms={
            "observations": {
                "start_time": "2026-08-10T00:00:00Z",
                "end_time": "2026-08-10T00:01:00Z",
                "pingmesh_metrics": {
                    "query_status": {"ok": True},
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "value": 100,
                            "timestamp": "2026-08-10T00:00:10Z",
                        },
                        {
                            "type": "packet_loss",
                            "src_name": "client3",
                            "dst_name": "client4",
                            "value": 100,
                            "timestamp": "2026-08-10T00:00:40Z",
                        },
                    ],
                },
            }
        }
    )

    evidence = evidence_from_public_observations(context)

    assert len(evidence) == 2
    assert {item.independence_key for item in evidence} == {"pingmesh-window:2026-08-10T00:00:00Z:2026-08-10T00:01:00Z"}
