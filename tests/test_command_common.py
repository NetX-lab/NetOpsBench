import json
import subprocess

from netopsbench.platform.topology.topology_utils import check_topology_deployed


def _write_topology(tmp_path, lab_name="dcn", devices=None):
    topology_dir = tmp_path / "topology"
    topology_dir.mkdir()
    (topology_dir / f"{lab_name}.clab.yaml").write_text(f"name: {lab_name}\n")
    (topology_dir / "topology.json").write_text(json.dumps({"name": lab_name, "devices": devices or {}}))
    return topology_dir


def test_check_topology_deployed_rejects_mismatched_running_containers(monkeypatch, tmp_path):
    topology_dir = _write_topology(
        tmp_path,
        devices={
            "spines": [{"name": "spine1"}, {"name": "spine2"}],
            "leafs": [{"name": "leaf1"}, {"name": "leaf2"}],
            "clients": [{"name": "client1"}, {"name": "client2"}],
        },
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="\n".join(
                [
                    "clab-dcn-spine1",
                    "clab-dcn-spine2",
                    "clab-dcn-leaf1",
                    "clab-dcn-leaf2",
                    "clab-dcn-leaf3",
                    "clab-dcn-leaf4",
                ]
            ),
            stderr="",
        ),
    )

    ok, message = check_topology_deployed(str(topology_dir))

    assert ok is False
    assert "do not match topology metadata" in message
    assert "missing=clab-dcn-client1" in message


def test_check_topology_deployed_accepts_exact_metadata_match(monkeypatch, tmp_path):
    topology_dir = _write_topology(
        tmp_path,
        devices={
            "spines": [{"name": "spine1"}],
            "leafs": [{"name": "leaf1"}],
            "clients": [{"name": "client1"}],
        },
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="\n".join(
                [
                    "clab-dcn-spine1",
                    "clab-dcn-leaf1",
                    "clab-dcn-client1",
                ]
            ),
            stderr="",
        ),
    )

    ok, message = check_topology_deployed(str(topology_dir))

    assert ok is True
    assert "Detected 3 running container(s)" in message
