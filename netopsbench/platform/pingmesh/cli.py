#!/usr/bin/env python3
"""CLI entrypoint for the Pingmesh probe agent.

Separated from :mod:`netopsbench.platform.pingmesh.agent` so the agent
class stays a pure library object. This module is also copied into each
client container by :mod:`netopsbench.platform.pingmesh.deploy`, so the
in-container ``python3 /tmp/pingmesh/run_pingmesh_agent.py ...`` invocation
keeps working when the package layout is not on ``sys.path``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

try:
    from netopsbench.platform.pingmesh.agent import PingmeshAgent
except ImportError:  # In-container deployment runs from /tmp/pingmesh/ flat files.
    from agent import PingmeshAgent  # type: ignore[no-redef]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netopsbench-pingmesh-agent",
        description="Run the Pingmesh probe agent against a pinglist JSON file.",
    )
    parser.add_argument(
        "pinglist",
        help="Path to the pinglist JSON file (probes definition).",
    )
    parser.add_argument(
        "interval",
        nargs="?",
        type=float,
        default=1.0,
        help="Probe cycle interval in seconds (default: 1.0).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Pingmesh agent until interrupted."""
    args = _build_parser().parse_args(argv)
    agent = PingmeshAgent(
        args.pinglist,
        interval=args.interval,
        min_interval=args.interval,
        max_interval=args.interval,
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        agent.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
