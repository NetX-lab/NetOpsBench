import json
import os
import shlex
import subprocess
import sys

import pytest

from netopsbench.platform.topology.planning import client_commands


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("gateway", ["192.168.1.1", "192.168.1.1; exit 99"])
def test_client_setup_order_and_fail_fast(tmp_path, fail_at, gateway):
    log = tmp_path / "calls.jsonl"
    stub = (
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "p=pathlib.Path(os.environ['SETUP_TEST_LOG'])\n"
        "n=len(p.read_text().splitlines())+1 if p.exists() else 1\n"
        "with p.open('a') as f: f.write(json.dumps([pathlib.Path(sys.argv[0]).name,*sys.argv[1:]])+'\\n')\n"
        "sys.exit(37 if n==int(os.environ['SETUP_TEST_FAIL']) else 0)\n"
    )
    for name in ["ip", "ethtool", "sysctl"]:
        p = tmp_path / name
        p.write_text(stub)
        p.chmod(0o755)
    commands = client_commands("192.168.1.2", gateway)
    assert len(commands) == 1
    env = {**os.environ, "PATH": str(tmp_path), "SETUP_TEST_LOG": str(log), "SETUP_TEST_FAIL": str(fail_at)}
    args = shlex.split(commands[0])
    args[0] = "/bin/sh"
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == (37 if fail_at else 0), result.stderr
    expected = [
        ["sysctl", "-w", "net.ipv4.ip_forward=0"],
        ["ip", "link", "set", "dev", "eth1", "mtu", "9232"],
        ["ip", "addr", "add", "192.168.1.2/30", "dev", "eth1"],
        ["ip", "route", "add", "192.168.0.0/16", "via", gateway],
        [
            "ethtool",
            "-K",
            "eth1",
            "rx",
            "off",
            "tx",
            "off",
            "tso",
            "off",
            "gso",
            "off",
            "gro",
            "off",
            "sg",
            "off",
            "tx-udp-segmentation",
            "off",
        ],
    ]
    assert [json.loads(line) for line in log.read_text().splitlines()] == expected[: fail_at or 5]
