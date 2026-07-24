"""CLI boundary for Python-owned worker deployment."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from netopsbench.models.profiles import ScaleRegistry
from netopsbench.platform.runtime.deployment import teardown_worker_lab, worker_from_cli, worker_from_topology
from netopsbench.platform.runtime.lifecycle import (
    deploy_worker_transactionally,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy or teardown one NetOpsBench worker")
    parser.add_argument("--scale-profile", action="append", default=[])
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument("scale")
    deploy.add_argument("topology_dir")
    deploy.add_argument("lab_name")
    deploy.add_argument("mgmt_subnet")
    deploy.add_argument("bucket", nargs="?", default="netopsbench")
    deploy.add_argument("--mgmt-network")
    teardown = subparsers.add_parser("teardown")
    teardown.add_argument("topology_dir")
    args = parser.parse_args(argv)
    registry = ScaleRegistry.with_builtins(args.scale_profile)
    if args.command == "teardown":
        teardown_worker_lab(worker_from_topology(args.topology_dir), registry)
        return 0

    worker = worker_from_cli(
        scale=args.scale,
        topology_dir=args.topology_dir,
        lab_name=args.lab_name,
        mgmt_subnet=args.mgmt_subnet,
        bucket=args.bucket,
        mgmt_network=args.mgmt_network,
        registry=registry,
    )
    deploy_worker_transactionally(worker, args.scale, registry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
