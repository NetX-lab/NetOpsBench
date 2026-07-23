"""CLI boundary for the optional generic simulator HTTP service."""

from __future__ import annotations

import argparse
from pathlib import Path

from netopsbench.platform.simulator.contracts import SimulatorConfig
from netopsbench.platform.simulator.service import SimulatorService, create_app
from netopsbench.sdk.core import NetOpsBench


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the NetOpsBench simulator service")
    parser.add_argument("--scenario", action="append", default=[], help="Scenario YAML; may be repeated")
    parser.add_argument("--scenario-dir", action="append", default=[], help="Directory of scenario YAML files")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--scale-profile", action="append", default=[])
    parser.add_argument("--event-log", type=Path)
    parser.add_argument("--max-active-runtimes", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Install NetOpsBench with the 'simulator' extra to run the service") from exc

    scenario_paths = [Path(value) for value in args.scenario]
    for directory in args.scenario_dir:
        scenario_paths.extend(sorted(Path(directory).glob("*.y*ml")))
    if not scenario_paths:
        parser.error("Provide at least one --scenario or --scenario-dir")

    with NetOpsBench(workspace=args.workspace, scale_profiles=args.scale_profile) as bench:
        scenarios = [bench.scenarios.load(path) for path in scenario_paths]
        service = SimulatorService(
            bench.simulators,
            scenarios,
            SimulatorConfig(max_active_runtimes=args.max_active_runtimes),
            event_log=args.event_log,
        )
        uvicorn.run(create_app(service), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
