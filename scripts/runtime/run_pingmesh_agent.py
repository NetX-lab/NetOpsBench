#!/usr/bin/env python3
"""Thin shim that delegates to :mod:`netopsbench.platform.pingmesh.cli`.

This file is copied into each client container by
:mod:`netopsbench.platform.pingmesh.deploy` so the agent can be launched
with ``python3 /tmp/pingmesh/run_pingmesh_agent.py <pinglist> [interval]``
even when the package layout is not on ``sys.path``.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    if __package__ in {None, ""}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from netopsbench.platform.pingmesh.cli import main
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cli import main  # type: ignore[no-redef]


if __name__ == "__main__":
    raise SystemExit(main())
