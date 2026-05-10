#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from netopsbench.platform.toolkit.fastmcp_server import run_server


if __name__ == '__main__':
    run_server()
