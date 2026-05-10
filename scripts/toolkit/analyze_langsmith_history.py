#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze historical LangSmith traces for one project and rank high-cost suspicious samples.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--project-name", help="LangSmith project name")
    target.add_argument("--project-id", help="LangSmith project UUID")
    parser.add_argument("--start-time", help="Inclusive ISO-8601 start time filter")
    parser.add_argument("--end-time", help="Inclusive ISO-8601 end time filter applied client-side")
    parser.add_argument("--limit", type=int, default=20, help="Number of ranked traces to print (default: 20)")
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Optional cap on raw runs fetched from the project",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def _require_langsmith() -> Any:
    try:
        from langsmith import Client
    except ImportError as exc:  # pragma: no cover - runtime guard
        raise SystemExit(
            "langsmith is not installed. Install optional agent dependencies with: pip install -e '.[agent]'"
        ) from exc

    api_key = str(os.environ.get("LANGSMITH_API_KEY", "")).strip()
    if not api_key:
        raise SystemExit("LANGSMITH_API_KEY is required to query historical traces")
    return Client


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO-8601 timestamp: {raw}") from exc


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _serialize_payload(payload: Any) -> tuple[str, bool]:
    if payload in (None, "", {}, []):
        return "", False
    if isinstance(payload, str):
        return payload, True
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), True
    except TypeError:
        return str(payload), True


def _run_id(run: Any) -> str:
    return str(_field(run, "id", ""))


def _trace_id(run: Any) -> str:
    return str(_field(run, "trace_id", _field(run, "id", "")))


def _run_name(run: Any) -> str:
    return str(_field(run, "name", "unknown"))


def _run_type(run: Any) -> str:
    return str(_field(run, "run_type", "unknown")).lower()


def _run_status(run: Any) -> str:
    status = _field(run, "status")
    if status not in (None, ""):
        return str(status)
    if _field(run, "error"):
        return "error"
    return "unknown"


def _run_start_time(run: Any) -> datetime | None:
    return _field(run, "start_time")


def _string_contains_inconclusive(*parts: Any) -> bool:
    merged = " ".join(str(part) for part in parts if part not in (None, ""))
    return "inconclusive" in merged.lower()


@dataclass
class ToolAggregate:
    run_count: int = 0
    total_output_bytes: int = 0
    total_output_chars: int = 0
    max_output_bytes: int = 0
    traces: set[str] = field(default_factory=set)
    top_trace_hits: int = 0


@dataclass
class TraceAggregate:
    trace_id: str
    root_run_id: str = ""
    root_name: str = ""
    root_status: str = "unknown"
    root_error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tool_output_bytes: int = 0
    tool_output_chars: int = 0
    tool_call_count: int = 0
    llm_run_count: int = 0
    tool_totals: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    has_error: bool = False
    has_inconclusive: bool = False
    first_start_time: datetime | None = None

    @property
    def is_suspicious(self) -> bool:
        return self.has_error or self.has_inconclusive

    def to_dict(self) -> dict[str, Any]:
        top_tools = sorted(self.tool_totals.items(), key=lambda item: item[1], reverse=True)[:3]
        return {
            "trace_id": self.trace_id,
            "root_run_id": self.root_run_id,
            "root_name": self.root_name,
            "root_status": self.root_status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tool_output_bytes": self.tool_output_bytes,
            "tool_output_chars": self.tool_output_chars,
            "tool_call_count": self.tool_call_count,
            "llm_run_count": self.llm_run_count,
            "has_error": self.has_error,
            "has_inconclusive": self.has_inconclusive,
            "top_tools": [{"tool": name, "output_bytes": total} for name, total in top_tools],
            "first_start_time": self.first_start_time.isoformat() if self.first_start_time else None,
        }


def _iter_runs(client: Any, args: argparse.Namespace, start_time: datetime | None) -> Iterable[Any]:
    kwargs: dict[str, Any] = {}
    if start_time is not None:
        kwargs["start_time"] = start_time
    if args.project_name:
        kwargs["project_name"] = args.project_name
    if args.project_id:
        kwargs["project_id"] = args.project_id
    runs = client.list_runs(**kwargs)
    if args.max_runs is not None:
        return itertools.islice(runs, args.max_runs)
    return runs


def _analyze_runs(runs: Iterable[Any], end_time: datetime | None) -> tuple[dict[str, TraceAggregate], dict[str, ToolAggregate], str]:
    traces: dict[str, TraceAggregate] = {}
    tools: dict[str, ToolAggregate] = defaultdict(ToolAggregate)
    saw_full_tool_outputs = False
    saw_preview_only = False

    for run in runs:
        started = _run_start_time(run)
        if end_time is not None and started is not None and started > end_time:
            continue

        trace_id = _trace_id(run)
        if not trace_id:
            continue
        aggregate = traces.setdefault(trace_id, TraceAggregate(trace_id=trace_id))
        if aggregate.first_start_time is None or (started is not None and started < aggregate.first_start_time):
            aggregate.first_start_time = started

        run_type = _run_type(run)
        run_name = _run_name(run)
        status = _run_status(run)
        error = str(_field(run, "error", "") or "")

        run_outputs = _field(run, "outputs")
        run_outputs_preview = _field(run, "outputs_preview")
        serialized_outputs, has_outputs = _serialize_payload(run_outputs)
        serialized_preview, has_preview = _serialize_payload(run_outputs_preview)

        if run_type == "tool":
            payload_text = serialized_outputs if has_outputs else serialized_preview
            payload_bytes = len(payload_text.encode("utf-8")) if payload_text else 0
            payload_chars = len(payload_text)
            tool_entry = tools[run_name]
            tool_entry.run_count += 1
            tool_entry.total_output_bytes += payload_bytes
            tool_entry.total_output_chars += payload_chars
            tool_entry.max_output_bytes = max(tool_entry.max_output_bytes, payload_bytes)
            tool_entry.traces.add(trace_id)

            aggregate.tool_call_count += 1
            aggregate.tool_output_bytes += payload_bytes
            aggregate.tool_output_chars += payload_chars
            aggregate.tool_totals[run_name] += payload_bytes
            aggregate.has_error = aggregate.has_error or bool(error) or status == "error"
            aggregate.has_inconclusive = aggregate.has_inconclusive or _string_contains_inconclusive(
                payload_text,
                error,
                _field(run, "extra"),
            )
            saw_full_tool_outputs = saw_full_tool_outputs or has_outputs
            saw_preview_only = saw_preview_only or (not has_outputs and has_preview)
            continue

        if run_type == "llm":
            aggregate.prompt_tokens += int(_field(run, "prompt_tokens", 0) or 0)
            aggregate.completion_tokens += int(_field(run, "completion_tokens", 0) or 0)
            aggregate.total_tokens += int(_field(run, "total_tokens", 0) or 0)
            aggregate.llm_run_count += 1
            aggregate.has_error = aggregate.has_error or bool(error) or status == "error"
            aggregate.has_inconclusive = aggregate.has_inconclusive or _string_contains_inconclusive(
                serialized_outputs,
                serialized_preview,
                error,
            )
            continue

        is_root = bool(_field(run, "parent_run_id") in (None, "")) or _field(run, "is_root") is True
        if is_root or aggregate.root_run_id == "":
            aggregate.root_run_id = _run_id(run)
            aggregate.root_name = run_name
            aggregate.root_status = status
            aggregate.root_error = error
        aggregate.has_error = aggregate.has_error or bool(error) or status == "error"
        aggregate.has_inconclusive = aggregate.has_inconclusive or _string_contains_inconclusive(
            serialized_outputs,
            serialized_preview,
            error,
            _field(run, "extra"),
        )

    if saw_full_tool_outputs:
        mode = "full_outputs"
    elif saw_preview_only:
        mode = "preview_only"
    else:
        mode = "no_tool_outputs"
    return traces, tools, mode


def _sort_traces(traces: dict[str, TraceAggregate]) -> list[TraceAggregate]:
    return sorted(
        traces.values(),
        key=lambda item: (
            int(item.is_suspicious),
            item.prompt_tokens,
            item.tool_output_bytes,
            item.tool_call_count,
        ),
        reverse=True,
    )


def _top_tools_for_trace(trace: TraceAggregate) -> str:
    top_tools = sorted(trace.tool_totals.items(), key=lambda item: item[1], reverse=True)[:3]
    if not top_tools:
        return "-"
    return ", ".join(f"{name}:{total}" for name, total in top_tools)


def _render_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(cells: list[str]) -> str:
        rendered = []
        for index, cell in enumerate(cells):
            if aligns[index] == ">":
                rendered.append(cell.rjust(widths[index]))
            else:
                rendered.append(cell.ljust(widths[index]))
        return "  ".join(rendered).rstrip()

    sep = "  ".join("-" * width for width in widths)
    return "\n".join([format_row(headers), sep, *[format_row(row) for row in rows]])


def _print_text(args: argparse.Namespace, ranked_traces: list[TraceAggregate], tool_summary: list[tuple[str, ToolAggregate]], mode: str) -> None:
    title_target = args.project_name or args.project_id
    print(f"LangSmith History Analysis  {title_target}")
    print("=" * max(32, len(title_target or "") + 26))
    print(f"mode: {mode}")
    if args.start_time:
        print(f"start_time: {args.start_time}")
    if args.end_time:
        print(f"end_time: {args.end_time}")
    print()

    top_traces = ranked_traces[: args.limit]
    trace_rows = []
    for trace in top_traces:
        flags = []
        if trace.has_error:
            flags.append("error")
        if trace.has_inconclusive:
            flags.append("inconclusive")
        trace_rows.append(
            [
                trace.trace_id[:8],
                trace.root_name or "-",
                trace.root_status,
                str(trace.prompt_tokens),
                str(trace.tool_output_bytes),
                str(trace.tool_call_count),
                _top_tools_for_trace(trace),
                ",".join(flags) or "-",
            ]
        )

    if trace_rows:
        print("Top High-Cost Suspicious Traces")
        print(
            _render_table(
                ["Trace", "Root", "Status", "PromptTok", "ToolBytes", "Tools", "TopTools", "Flags"],
                trace_rows,
                ["<", "<", "<", ">", ">", ">", "<", "<"],
            )
        )
        print()
    else:
        print("No traces matched the requested filters")
        return

    tool_rows = []
    for tool_name, aggregate in tool_summary[: args.limit]:
        tool_rows.append(
            [
                tool_name,
                str(aggregate.run_count),
                str(aggregate.total_output_bytes),
                str(aggregate.max_output_bytes),
                str(len(aggregate.traces)),
                str(aggregate.top_trace_hits),
            ]
        )
    if tool_rows:
        print("Supporting Tool Summary")
        print(
            _render_table(
                ["Tool", "Runs", "TotalBytes", "MaxBytes", "Traces", "TopTraceHits"],
                tool_rows,
                ["<", ">", ">", ">", ">", ">"],
            )
        )
        print()

    if mode == "preview_only":
        print("note: tool output ranking uses outputs_preview because full outputs were not available")
    elif mode == "no_tool_outputs":
        print("note: no tool outputs were available; ranking is driven only by LLM token usage and suspicious flags")


def _build_json_payload(
    args: argparse.Namespace,
    ranked_traces: list[TraceAggregate],
    tool_summary: list[tuple[str, ToolAggregate]],
    mode: str,
) -> dict[str, Any]:
    top_trace_ids = {trace.trace_id for trace in ranked_traces[: args.limit]}
    return {
        "project_name": args.project_name,
        "project_id": args.project_id,
        "analysis_mode": mode,
        "start_time": args.start_time,
        "end_time": args.end_time,
        "trace_count": len(ranked_traces),
        "top_traces": [trace.to_dict() for trace in ranked_traces[: args.limit]],
        "tool_summary": [
            {
                "tool": tool_name,
                "run_count": aggregate.run_count,
                "total_output_bytes": aggregate.total_output_bytes,
                "total_output_chars": aggregate.total_output_chars,
                "max_output_bytes": aggregate.max_output_bytes,
                "trace_count": len(aggregate.traces),
                "top_trace_hits": aggregate.top_trace_hits,
            }
            for tool_name, aggregate in tool_summary
        ],
        "top_trace_ids": sorted(top_trace_ids),
    }


def main() -> int:
    args = _parse_args()
    start_time = _parse_time(args.start_time)
    end_time = _parse_time(args.end_time)
    Client = _require_langsmith()
    client = Client()

    runs = _iter_runs(client, args, start_time)
    traces, tools, mode = _analyze_runs(runs, end_time)
    ranked_traces = _sort_traces(traces)

    for trace in ranked_traces[: args.limit]:
        for tool_name in trace.tool_totals:
            tools[tool_name].top_trace_hits += 1
    tool_summary = sorted(tools.items(), key=lambda item: item[1].total_output_bytes, reverse=True)

    if args.json:
        print(json.dumps(_build_json_payload(args, ranked_traces, tool_summary, mode), indent=2, ensure_ascii=False))
    else:
        _print_text(args, ranked_traces, tool_summary, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())