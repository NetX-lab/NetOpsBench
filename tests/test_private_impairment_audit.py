"""Impairment configuration stays in private validation, outside agent tools."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.scenario.effects import validate_effect
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import config_for_scale


@pytest.mark.parametrize("fault", ["packet_loss", "packet_corruption", "high_latency"])
@pytest.mark.parametrize("collection", ["ok", "failed", "invalid_json"])
def test_impairment_effect_uses_private_qdisc_collection(monkeypatch, fault, collection):
    manifest = build_clos_plan(config_for_scale("medium")).manifest
    pair = {"src": "client1", "dst": "client3"}
    episode = EpisodeSpec(
        episode_id="audit",
        fault_type=fault,
        target_device="leaf1",
        target_interface="Ethernet0",
        parameters={"affected_candidates": [pair]},
    )
    calls = []

    def interfaces(device, interface):
        # No include_tc keyword is accepted, even for internal validation.
        assert device == "leaf1" and interface == "Ethernet0"
        return ToolResult(success=True, data={"interfaces": []})

    def run(container, args):
        calls.append((container, args))
        return subprocess.CompletedProcess(
            args,
            1 if collection == "failed" else 0,
            stdout="invalid" if collection == "invalid_json" else json.dumps([{"kind": "netem"}]),
            stderr="unavailable" if collection == "failed" else "",
        )

    toolkit = SimpleNamespace(
        get_device_status=lambda device: ToolResult(success=True, data={"running": True, "accessible": True}),
        get_device_interfaces=interfaces,
    )
    monkeypatch.setattr("netopsbench.platform.scenario.effects.AgentToolkit", lambda **kw: toolkit)
    executor = SimpleNamespace(
        current_manifest=lambda: manifest,
        influxdb_bucket="worker",
        topology_id=manifest.topology_id,
        injector=SimpleNamespace(
            _iface=SimpleNamespace(resolve_sonic=lambda name: name, resolve_linux=lambda name: "eth1"),
            _cmd=SimpleNamespace(docker_exec=run),
            container_names={"leaf1": "worker-leaf1"},
        ),
        _case_affected_pairs=[pair],
    )
    probes = [
        {
            **pair,
            "baseline": {"samples": 20, "regular": {"lost": 0, "loss_pct": 0}, "rtt_max_ms": 1},
            "fault": {"samples": 20, "regular": {"lost": 5, "loss_pct": 25}, "rtt_max_ms": 150},
        }
    ]
    result = validate_effect(
        executor, episode, {"observations": {"_probe_evidence": probes}}, {"nodes": {}}, {"nodes": {}}
    )
    assert calls == [("worker-leaf1", ["tc", "-s", "-j", "qdisc", "show", "dev", "eth1"])]
    assert result["valid"] is (collection == "ok")
    assert "qdisc" not in result["evidence"]["interface"]["data"]
    if collection == "ok":
        assert result["evidence"]["qdisc"]["rows"] == [{"kind": "netem"}]
    else:
        assert any("Private qdisc collection failed" in error for error in result["errors"])
