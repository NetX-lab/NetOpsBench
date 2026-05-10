"""
Integration tests that touch a **deployed** lab, Docker, InfluxDB, and Grafana.

All cases are marked ``@pytest.mark.real``. Default CI uses ``pytest -m "not real"`` and skips these.

Run (after ``bash scripts/runtime/deploy.sh ...`` and observability up):

    export NETOPSBENCH_TOPOLOGY_DIR=/path/to/lab-topology/generated_topology_xs

Optional env for Pingmesh/Influx tests (override placeholder token in tests if needed):

    export NETOPSBENCH_INFLUXDB_TOKEN=...

    pytest tests/test_e2e_real.py -m real -v

Cheap script/CLI checks live in ``tests/test_scripts_and_cli_smoke.py`` (default CI).
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timedelta
from io import StringIO

import pytest
import requests

from netopsbench.platform.pingmesh.detector import AnomalyDetector
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.traffic.controller import TrafficController, TrafficFlow


def _toolkit_or_skip() -> AgentToolkit:
    try:
        return AgentToolkit()
    except FileNotFoundError as exc:
        pytest.skip(
            "AgentToolkit requires topology metadata (e.g. set NETOPSBENCH_TOPOLOGY_DIR "
            f"to generated_topology_* after deploy): {exc}"
        )


@pytest.mark.real
class TestPingmeshIntegration:
    """Tests for Pingmesh anomaly detection (InfluxDB on localhost)."""

    @staticmethod
    def _influx_token() -> str:
        return os.environ.get("NETOPSBENCH_INFLUXDB_TOKEN") or "replace-me"

    def test_pingmesh_anomaly_detector(self):
        """
        Requires:
        - InfluxDB running at localhost:8086
        - Pingmesh data in netopsbench bucket
        """
        detector = AnomalyDetector(
            influxdb_url="http://localhost:8086",
            token=self._influx_token(),
            org="netopsbench",
            bucket="netopsbench",
        )

        now = datetime.utcnow().replace(microsecond=0)
        baseline_start = (now - timedelta(minutes=10)).isoformat() + "Z"
        baseline_end = (now - timedelta(minutes=5)).isoformat() + "Z"
        current_start = baseline_end
        current_end = now.isoformat() + "Z"

        report = detector.generate_anomaly_report(
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
        )

        assert "timestamp" in report
        assert "summary" in report
        assert "anomalies" in report
        assert "aggregated_anomalies" in report

        assert "total_anomalies" in report["summary"]
        assert "latency_spikes" in report["summary"]
        assert "packet_loss_events" in report["summary"]

        assert "by_src_leaf" in report["aggregated_anomalies"]
        assert "by_dst_leaf" in report["aggregated_anomalies"]

    def test_latency_anomaly_detection(self):
        detector = AnomalyDetector(
            influxdb_url="http://localhost:8086",
            token=self._influx_token(),
            org="netopsbench",
            bucket="netopsbench",
        )

        now = datetime.utcnow().replace(microsecond=0)
        baseline_start = (now - timedelta(minutes=10)).isoformat() + "Z"
        baseline_end = (now - timedelta(minutes=5)).isoformat() + "Z"
        current_start = baseline_end
        current_end = now.isoformat() + "Z"

        anomalies = detector.detect_latency_anomalies(
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
        )

        assert isinstance(anomalies, list)

        for anomaly in anomalies:
            assert anomaly.type == "latency_spike"
            assert anomaly.src_ip
            assert anomaly.dst_ip
            assert anomaly.value >= 0
            assert anomaly.baseline >= 0
            assert anomaly.severity in ["low", "medium", "high"]

    def test_packet_loss_detection(self):
        detector = AnomalyDetector(
            influxdb_url="http://localhost:8086",
            token=self._influx_token(),
            org="netopsbench",
            bucket="netopsbench",
        )

        now = datetime.utcnow().replace(microsecond=0)
        baseline_start = (now - timedelta(minutes=10)).isoformat() + "Z"
        baseline_end = (now - timedelta(minutes=5)).isoformat() + "Z"
        current_start = baseline_end
        current_end = now.isoformat() + "Z"

        anomalies = detector.detect_packet_loss(
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
        )

        assert isinstance(anomalies, list)

        for anomaly in anomalies:
            assert anomaly.type == "packet_loss"
            assert anomaly.src_ip
            assert anomaly.dst_ip
            assert anomaly.value > 0


@pytest.mark.real
class TestTrafficGenerationLive:
    """TrafficController against real client containers (clab-dcn-*)."""

    def test_traffic_controller_basic(self):
        container_names = {
            "client1": "clab-dcn-client1",
            "client2": "clab-dcn-client2",
        }

        controller = TrafficController(container_names)

        flow = TrafficFlow(
            src="client1",
            dst="client2",
            dst_ip="192.168.2.2",
            bandwidth="100M",
            duration=5,
        )

        flow_id = controller.start_flow(flow)
        assert flow_id == flow.flow_id
        assert flow_id in controller.active_flows

        time.sleep(6)

        controller.stop_flow(flow_id)
        assert flow_id not in controller.active_flows


@pytest.mark.real
class TestObservabilityNormal:
    """Pingmesh summary, Grafana panel proxy, and datasource checks."""

    @pytest.fixture(scope="class")
    def toolkit(self):
        return _toolkit_or_skip()

    def _csv_has_values(self, csv_text: str) -> bool:
        reader = csv.DictReader(StringIO(csv_text))
        for row in reader:
            if row.get("_value"):
                return True
        return False

    def test_pingmesh_summary_has_data(self, toolkit):
        result = toolkit.get_pingmesh_summary(time_range_minutes=10)
        assert result.success is True, result.error

        rows = result.data.get("rows", [])
        assert len(rows) > 0, "No pingmesh rows found in recent window"

        summary = result.data.get("path_type_summary", {})
        assert any(
            (values or {}).get("rtt_p99") is not None or (values or {}).get("packet_loss") is not None
            for values in summary.values()
        ), "Pingmesh summary has no numeric values"

    def test_grafana_datasource_configured(self, toolkit):
        health = requests.get(
            f"{toolkit.grafana_url}/api/health",
            auth=toolkit.grafana_auth,
            timeout=10,
            proxies={"http": "", "https": ""},
        )
        assert health.status_code == 200

        datasource = requests.get(
            f"{toolkit.grafana_url}/api/datasources/uid/InfluxDB_v2",
            auth=toolkit.grafana_auth,
            timeout=10,
            proxies={"http": "", "https": ""},
        )
        assert datasource.status_code == 200
        payload = datasource.json()
        assert payload.get("type") == "influxdb"
        assert payload.get("uid") == "InfluxDB_v2"
        assert payload.get("jsonData", {}).get("defaultBucket") == "netopsbench"


if __name__ == "__main__":
    pytest.main([__file__, "-m", "real", "-v"])
