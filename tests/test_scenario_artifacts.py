"""Large private snapshots remain complete without inflating primary result JSON."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from netopsbench.platform.scenario.artifacts import persist_result


def test_private_snapshots_are_lossless_and_do_not_mutate_execution_or_scores(tmp_path):
    snapshot = {
        "collected_at": "2026-10-12T12:00:00Z",
        "nodes": {
            "edge1": {
                "fib": [{"dst": "192.168.1.0/30", "nexthops": [{"gateway": "10.0.0.1", "dev": "Ethernet0"}]}] * 1000
            }
        },
    }
    diagnosis = {"success": True, "fault_type": "ecmp_misconfig"}
    result = {
        "case_valid": True,
        "episode": {"diagnosis": diagnosis, "route_evidence": {"baseline": snapshot, "fault": snapshot}},
        "cleanup": {"success": True, "route_recovery": snapshot},
    }
    path = tmp_path / "case.json"
    persist_result(path, result)
    saved = json.loads(path.read_text())
    assert saved["episode"]["diagnosis"] == diagnosis
    assert saved["case_valid"] is True and saved["cleanup"]["success"] is True
    assert result["episode"]["route_evidence"]["baseline"] is snapshot
    assert result["cleanup"]["route_recovery"] is snapshot
    refs = [*saved["episode"]["route_evidence"].values(), saved["cleanup"]["route_recovery"]]
    for ref in refs:
        artifact = Path(ref["path"])
        data = artifact.read_bytes()
        assert artifact.is_relative_to(tmp_path)
        assert ref["encoding"] == "gzip+json"
        assert hashlib.sha256(data).hexdigest() == ref["sha256"]
        assert json.loads(gzip.decompress(data)) == snapshot
        assert ref["compressed_bytes"] < ref["uncompressed_bytes"] / 10
    assert path.stat().st_size < 3000


def test_evidence_write_failure_keeps_primary_result_and_leaves_no_partial_file(tmp_path, monkeypatch):
    from netopsbench.platform.utils import files

    path = tmp_path / "case.json"
    path.write_text('{"original": true}')
    original = files.os.replace

    def fail_evidence(source, target):
        if str(target).endswith(".json.gz"):
            raise OSError("storage failure")
        original(source, target)

    monkeypatch.setattr(files.os, "replace", fail_evidence)
    with pytest.raises(OSError, match="storage failure"):
        persist_result(path, {"episode": {"route_evidence": {"baseline": {"nodes": {}}}}})
    assert json.loads(path.read_text()) == {"original": True}
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob("*.json.gz"))


def test_result_without_route_snapshots_keeps_the_same_json_contract(tmp_path):
    result = {"success": True, "episode": {"diagnosis": {"verdict": "network_healthy"}}}
    path = tmp_path / "case.json"
    persist_result(path, result)
    assert json.loads(path.read_text()) == result
    assert not (tmp_path / "evidence").exists()
