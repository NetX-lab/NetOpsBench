"""Trace CLI commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from netopsbench.sdk import NetOpsBench


def add_trace_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register trace subcommands on the root CLI parser."""

    trace_parser = subparsers.add_parser("trace", help="Inspect and export agent runtime traces")
    trace_sub = trace_parser.add_subparsers(dest="trace_action", required=True)
    trace_list = trace_sub.add_parser("list", help="List runs with trace artifacts")
    trace_list.add_argument(
        "--limit", type=int, default=20, help="Maximum number of runs to show. Default: %(default)s."
    )
    trace_export = trace_sub.add_parser("export", help="Export run traces as a Harbor jobs directory")
    trace_export.add_argument("run_id", help="Run id, for example run-20260605T124040Z")
    trace_export.add_argument("--output", required=True, help="Output Harbor jobs directory")
    trace_view = trace_sub.add_parser("view", help="Sync run traces and open them in Harbor viewer")
    trace_view.add_argument(
        "run_id",
        nargs="?",
        help="Optional run id, for example run-20260605T124040Z, or latest. Defaults to the jobs overview.",
    )
    trace_view.add_argument(
        "--output",
        help="Output Harbor jobs directory (default: <workspace>/.netopsbench/harbor-jobs)",
    )
    trace_view.add_argument("--host", default="127.0.0.1", help="Host to bind the viewer to")
    trace_view.add_argument("--port", default="8080-8089", help="Port or port range for the viewer")


def cmd_trace(bench: NetOpsBench, args: argparse.Namespace) -> int:
    """Handle ``netopsbench trace`` subcommands."""

    if args.trace_action == "list":
        rows = _collect_trace_runs(bench)
        if not rows:
            print("no runs found")
            return 0
        limit = max(1, int(args.limit))
        rows = rows[:limit]
        table_rows = [
            (
                row["run_id"],
                row["status"],
                row["scale"],
                row["agent"],
                row["provider"],
                row["model"],
                "yes" if row["has_trace"] else "no",
                row["completed_at"] or row["started_at"] or "-",
            )
            for row in rows
        ]
        headers = ("RUN ID", "STATUS", "SCALE", "AGENT", "PROVIDER", "MODEL", "TRACE", "COMPLETED")
        widths = [max(len(h), max(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        for row in table_rows:
            print(fmt.format(*row))
        return 0
    if args.trace_action == "export":
        run_id = _resolve_trace_run_id(bench, args.run_id)
        if run_id is None:
            print("no trace-enabled runs found")
            return 1
        raw_output = Path(args.output)
        output_path = raw_output if raw_output.is_absolute() else (bench.workspace / raw_output)
        try:
            exported = bench.artifacts.export_traces(run_id, output=output_path)
        except Exception as exc:
            print(f"trace export failed: {exc}")
            return 1
        print(f"exported traces: {exported}")
        return 0
    if args.trace_action == "view":
        trace_rows = [row for row in _collect_trace_runs(bench) if row["has_trace"]]
        if not trace_rows:
            print("no trace-enabled runs found")
            return 1
        run_id = _resolve_trace_run_id(bench, args.run_id) if args.run_id else None
        if args.run_id and run_id is None:
            print("no trace-enabled runs found")
            return 1
        if run_id and run_id not in {str(row["run_id"]) for row in trace_rows}:
            print(f"trace-enabled run not found: {run_id}")
            return 1
        output_path = _trace_view_output_path(bench, args.output)
        synced = _sync_trace_exports(bench, trace_rows, output=output_path)
        print(f"synced traces: {synced['exported']} exported -> {output_path}")
        failures = synced["failures"]
        for failed_run_id, error in failures:
            print(f"warning: failed to sync {failed_run_id}: {error}")
        if run_id and any(failed_run_id == run_id for failed_run_id, _ in failures):
            return 1
        try:
            _launch_harbor_viewer(output_path, host=args.host, port=args.port)
        except KeyboardInterrupt:
            print("trace viewer stopped")
            return 130
        except SystemExit as exc:
            return int(exc.code or 0)
        except Exception as exc:
            print(f"trace viewer failed: {exc}")
            print(f"you can still open it manually with: harbor view {output_path} --jobs")
            return 1
        return 0
    raise AssertionError(f"unhandled trace action: {args.trace_action}")


def _resolve_trace_run_id(bench: NetOpsBench, raw_run_id: str) -> str | None:
    if raw_run_id != "latest":
        return raw_run_id
    for row in _collect_trace_runs(bench):
        if row["has_trace"]:
            return row["run_id"]
    return None


def _sync_trace_exports(
    bench: NetOpsBench,
    rows: list[dict[str, str | bool]],
    *,
    output: Path,
) -> dict[str, object]:
    exported = 0
    failures: list[tuple[str, str]] = []
    for row in rows:
        run_id = str(row["run_id"])
        try:
            bench.artifacts.export_traces(run_id, output=output)
            exported += 1
        except Exception as exc:  # noqa: BLE001 - keep viewer usable when one old run is corrupt
            failures.append((run_id, str(exc)))
    return {"exported": exported, "failures": failures}


def _collect_trace_runs(bench: NetOpsBench) -> list[dict[str, str | bool]]:
    runs_dir = bench.workspace / ".netopsbench" / "runs"
    if not runs_dir.is_dir():
        return []
    rows: list[dict[str, str | bool]] = []
    for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        payload = _load_json_object(run_dir / "report.json") or _load_json_object(run_dir / "metadata.json") or {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        trace_rows = _load_jsonl_objects(run_dir / "traces" / "index.jsonl")
        trace_probe = _first_trace_metadata_row(trace_rows)
        raw_run_id = payload.get("run_id") or payload.get("id") or run_dir.name
        run_id = str(raw_run_id)
        if run_id.startswith("run:"):
            run_id = run_id[4:]
        rows.append(
            {
                "run_id": run_id,
                "status": str(payload.get("status") or summary.get("status") or "unknown"),
                "scale": str(
                    payload.get("topology_scale")
                    or summary.get("topology_scale")
                    or trace_probe.get("topology_scale")
                    or "unknown"
                ),
                "agent": str(payload.get("agent_name") or summary.get("agent_name") or "unknown"),
                "provider": str(trace_probe.get("provider") or "-"),
                "model": str(trace_probe.get("model") or "unknown"),
                "started_at": str(payload.get("started_at") or summary.get("started_at") or ""),
                "completed_at": str(payload.get("completed_at") or summary.get("completed_at") or ""),
                "has_trace": (run_dir / "traces" / "index.jsonl").exists(),
                "sort_key": str(
                    payload.get("completed_at")
                    or summary.get("completed_at")
                    or payload.get("started_at")
                    or summary.get("started_at")
                    or run_dir.stat().st_mtime
                ),
            }
        )
    return sorted(rows, key=lambda row: str(row["sort_key"]), reverse=True)


def _first_trace_metadata_row(rows: list[dict[str, object]]) -> dict[str, object]:
    for row in rows:
        if row.get("provider") or row.get("model") or row.get("topology_scale"):
            return row
    return rows[0] if rows else {}


def _load_json_object(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _trace_view_output_path(bench: NetOpsBench, raw_output: str | None) -> Path:
    if raw_output:
        output_path = Path(raw_output)
        return output_path if output_path.is_absolute() else (bench.workspace / output_path)
    return bench.workspace / ".netopsbench" / "harbor-jobs"


def _launch_harbor_viewer(folder: Path, *, host: str, port: str) -> None:
    from harbor.cli.main import view_command

    view_command(Path(folder), host=host, port=port, jobs=True)
