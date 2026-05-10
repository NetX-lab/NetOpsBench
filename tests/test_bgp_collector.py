from pathlib import Path

from scripts.runtime.run_bgp_collector import build_bgp_lines, collect_bgp_lines, normalize_bgp_state


def test_build_bgp_lines_normalizes_states_and_fields():
    lines = build_bgp_lines(
        "spine1",
        [
            {
                "neighbor": "192.168.11.2",
                "asn": 65011,
                "state": "Established",
                "prefixes_received": 2,
                "up_down": "04:54:34",
                "msg_rcvd": 310,
                "msg_sent": 309,
                "in_q": 0,
                "out_q": 0,
            }
        ],
        123456789,
    )

    assert len(lines) == 1
    assert lines[0].startswith("bgp_neighbors,source=spine1,neighbor_address=192.168.11.2 ")
    assert 'session_state="ESTABLISHED"' in lines[0]
    assert "asn=65011i" in lines[0]
    assert "prefixes_received=2i" in lines[0]
    assert lines[0].endswith(" 123456789")


def test_normalize_bgp_state_defaults_to_unknown():
    assert normalize_bgp_state(None) == "UNKNOWN"
    assert normalize_bgp_state("Idle") == "IDLE"


def test_collect_bgp_lines_reads_topology_and_executes_docker(monkeypatch, tmp_path):
    metadata_file = tmp_path / "topology.json"
    metadata_file.write_text(
        '{"name":"demo","devices":{"spines":[{"name":"spine1"}],"leafs":[{"name":"leaf1"}]}}',
        encoding="utf-8",
    )

    calls = []

    class _Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(args, capture_output, text, check, timeout):
        calls.append(args)
        return _Result("""
Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
192.168.11.2    4      65011       310       309       20    0    0 04:54:34            2       16 N/A
""")

    monkeypatch.setattr("scripts.runtime.run_bgp_collector.subprocess.run", fake_run)
    monkeypatch.setattr("scripts.runtime.run_bgp_collector._docker_prefix", lambda: [])

    lines = collect_bgp_lines(Path(metadata_file), timestamp_ns=7)

    assert len(lines) == 2
    assert calls[0][:3] == ["docker", "exec", "clab-demo-spine1"]
    assert calls[1][:3] == ["docker", "exec", "clab-demo-leaf1"]
    assert all('session_state="ESTABLISHED"' in line for line in lines)
