"""Public report persistence helpers."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from netopsbench.sdk.exceptions import RunFailedError


class BenchmarkReport:
    """Thin serializable benchmark report wrapper."""

    def __init__(
        self,
        id: str | None = None,
        summary: dict[str, Any] | None = None,
        scenario_summaries: list[dict[str, Any]] | None = None,
        detailed_results: list[dict[str, Any]] | None = None,
        artifact_paths: dict[str, str] | None = None,
        raw: dict[str, Any] | None = None,
        *,
        report_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ):
        if id is None and report_id is not None:
            payload_dict = dict(payload or {})
            id = report_id
            summary = dict(payload_dict.get("summary") or {})
            for key in ("mode", "status", "runtime_id"):
                if key in payload_dict and key not in summary:
                    summary[key] = payload_dict[key]
            scenario_summaries = list(payload_dict.get("scenario_summaries") or [])
            if not scenario_summaries:
                scenario_summaries = [
                    {"scenario_id": scenario_id} for scenario_id in payload_dict.get("scenario_ids", [])
                ]
            detailed_results = list(payload_dict.get("detailed_results") or payload_dict.get("results") or [])
            artifact_paths = dict(payload_dict.get("artifact_paths") or {})
            raw = dict(payload_dict)

        self.id = str(id or "")
        self.summary = dict(summary or {})
        self.scenario_summaries = [dict(item) for item in (scenario_summaries or [])]
        self.detailed_results = [dict(item) for item in (detailed_results or [])]
        self.artifact_paths = {str(key): str(value) for key, value in dict(artifact_paths or {}).items()}
        self.raw = dict(raw or {})

    @property
    def report_id(self) -> str:
        return self.id

    @property
    def payload(self) -> dict[str, Any]:
        payload = dict(self.summary)
        if self.scenario_summaries:
            payload["scenario_summaries"] = [dict(item) for item in self.scenario_summaries]
        if self.detailed_results:
            payload["detailed_results"] = [dict(item) for item in self.detailed_results]
        if self.artifact_paths:
            payload["artifact_paths"] = dict(self.artifact_paths)
        if self.raw:
            payload.update(dict(self.raw))
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": dict(self.summary),
            "scenario_summaries": [dict(item) for item in self.scenario_summaries],
            "detailed_results": [dict(item) for item in self.detailed_results],
            "artifact_paths": dict(self.artifact_paths),
            "raw": dict(self.raw),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)

    def save(self, path: Path) -> None:
        report_path = Path(path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> BenchmarkReport:
        report_path = Path(path)
        if not report_path.exists():
            raise FileNotFoundError(f"report file not found: {report_path}")
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid report JSON: {report_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Report payload must be a JSON object: {report_path}")
        if "report_id" in payload or "payload" in payload:
            report_id = payload.get("report_id")
            report_payload = payload.get("payload")
            if not isinstance(report_id, str) or not isinstance(report_payload, dict):
                raise ValueError(f"Report payload must contain string report_id and object payload: {report_path}")
            return cls(report_id=report_id, payload=report_payload)
        report_id = payload.get("id")
        summary = payload.get("summary")
        if not isinstance(report_id, str) or not isinstance(summary, dict):
            raise ValueError(f"Report payload must contain string id and object summary: {report_path}")
        return cls(
            id=report_id,
            summary=summary,
            scenario_summaries=payload.get("scenario_summaries", []),
            detailed_results=payload.get("detailed_results", []),
            artifact_paths=payload.get("artifact_paths", {}),
            raw=payload.get("raw", {}),
        )

    def pretty_print(self, *, json: bool = False) -> None:
        """Render the report to stdout in a human-readable form.

        Sections (in order, skipped when empty):
          1. Header — id, mode, status, runtime, time window.
          2. Per-case table — one row per scenario in ``detailed_results``.
          3. Summary — aggregate counters / accuracy / averages.
          4. Footer — artifact paths.

        Pass ``json=True`` to restore the legacy raw-JSON dump for
        machine consumers.
        """
        if json:
            print(self.to_json())
            return

        sections: list[str] = []
        header = _format_header(self)
        if header:
            sections.append(header)
        table = _format_per_case_table(self.detailed_results)
        if table:
            sections.append(table)
        summary_block = _format_summary_block(self.summary)
        if summary_block:
            sections.append(summary_block)
        footer = _format_footer(self.artifact_paths)
        if footer:
            sections.append(footer)
        judgments = _format_fault_type_judgments(self.detailed_results)
        if judgments:
            sections.append(judgments)

        if not sections:
            print(self.to_json())
            return
        print("\n\n".join(sections))


# ---------------------------------------------------------------------------
# Pretty-printing helpers (stdlib-only ASCII formatting).
# ---------------------------------------------------------------------------


def _short_scenario_id(scenario_id: str) -> str:
    sid = str(scenario_id or "")
    if sid.startswith("generated_"):
        sid = sid[len("generated_") :]
    # Strip trailing scale + numeric suffix like "_xs_001".
    parts = sid.rsplit("_", 2)
    if len(parts) == 3 and parts[-2] in {"xs", "small", "medium", "large"} and parts[-1].isdigit():
        sid = parts[0]
    return sid


def _flag(value: Any) -> str:
    if value is True:
        return "Y"
    if value is False:
        return "N"
    return "-"


def _fmt_number(value: Any, spec: str) -> str:
    if value is None:
        return "-"
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return str(value)


def _truncate(text: str, width: int) -> str:
    s = "" if text is None else str(text)
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "~"


def _render_table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def fmt_row(cells: list[str]) -> str:
        out = []
        for i in range(cols):
            cell = cells[i] if i < len(cells) else ""
            if aligns[i] == ">":
                out.append(cell.rjust(widths[i]))
            else:
                out.append(cell.ljust(widths[i]))
        return "  ".join(out).rstrip()

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    lines.extend(fmt_row(r) for r in rows)
    return "\n".join(lines)


def _format_header(report: BenchmarkReport) -> str:
    summary = report.summary or {}
    title = f"Benchmark Report  {report.id}".strip()
    bar = "=" * max(len(title), 60)
    fields: list[tuple[str, Any]] = [
        ("mode", summary.get("mode")),
        ("status", summary.get("status")),
        ("runtime", summary.get("runtime_id")),
        ("started", summary.get("started_at")),
        ("completed", summary.get("completed_at")),
    ]
    lines = [bar, title, bar]
    for key, value in fields:
        if value not in (None, ""):
            lines.append(f"  {key:<10s} {value}")
    return "\n".join(lines)


def _format_per_case_table(detailed: list[dict[str, Any]]) -> str:
    if not detailed:
        return ""
    headers = [
        "#",
        "Scenario",
        "GT type",
        "GT dev:if",
        "Pred type",
        "Pred dev:if",
        "V",
        "D",
        "F",
        "I",
        "Score",
        "Time",
        "Tools",
    ]
    aligns = ["<", "<", "<", "<", "<", "<", "<", "<", "<", "<", ">", ">", ">"]
    rows: list[list[str]] = []
    for idx, case in enumerate(detailed, start=1):
        details = case.get("details") or {}
        gt = details.get("ground_truth") or {}
        gt_loc = gt.get("location") or {}
        agent_out = details.get("agent_output") or {}
        pred_loc = agent_out.get("location") or {}

        gt_dev = gt_loc.get("device") or "-"
        gt_if = gt_loc.get("interface") or "-"
        pred_dev = pred_loc.get("device") or "-"
        pred_if = pred_loc.get("interface") or "-"

        if details.get("inconclusive"):
            pred_type = "inconclusive"
        else:
            pred_type = agent_out.get("fault_type") or "-"

        score = case.get("score")
        time_taken = details.get("time_taken")
        if time_taken is None:
            time_taken = agent_out.get("time_taken_seconds")
        tool_calls = details.get("tool_calls_count")
        if tool_calls is None:
            tool_calls = agent_out.get("tool_calls_count")
            if tool_calls is None:
                tc = agent_out.get("tool_calls") or []
                tool_calls = len(tc) if isinstance(tc, list) else None

        # Interface flag: render "-" when not applicable to the case.
        if details.get("interface_applicable") is False:
            iflag = "-"
        else:
            iflag = _flag(case.get("correct_interface"))

        rows.append(
            [
                str(idx),
                _truncate(_short_scenario_id(case.get("scenario_id") or details.get("scenario_id") or ""), 30),
                _truncate(str(gt.get("fault_type") or "-"), 22),
                _truncate(f"{gt_dev}:{gt_if}", 22),
                _truncate(str(pred_type), 22),
                _truncate(f"{pred_dev}:{pred_if}", 22),
                _flag(case.get("correct_verdict")),
                _flag(case.get("correct_device")),
                _flag(case.get("correct_fault_type")),
                iflag,
                _fmt_number(score, ".2f"),
                _fmt_number(time_taken, ".1f"),
                _fmt_number(tool_calls, "d"),
            ]
        )

    title = f"Per-case Breakdown  ({len(detailed)} cases)"
    legend = "Legend: V=verdict  D=device  F=fault_type  I=interface (Y/N/-)"
    return f"{title}\n{'-' * len(title)}\n{_render_table(headers, aligns, rows)}\n{legend}"


_SUMMARY_LAYOUT: list[tuple[str, str, str]] = [
    # (key, label, format)
    ("total_cases", "Total cases", "d"),
    ("correct_verdict", "Correct verdict", "d"),
    ("correct_device", "Correct device", "d"),
    ("correct_fault_type", "Correct fault_type", "d"),
    ("correct_interface", "Correct interface", "d"),
    ("interface_applicable_cases", "Interface-applicable", "d"),
    ("detection_accuracy", "Detection accuracy", ".3f"),
    ("detection_precision", "Detection precision", ".3f"),
    ("detection_recall", "Detection recall", ".3f"),
    ("detection_f1", "Detection F1", ".3f"),
    ("detection_macro_f1", "Detection macro-F1", ".3f"),
    ("device_accuracy", "Device accuracy", ".3f"),
    ("fault_type_accuracy", "Fault-type accuracy", ".3f"),
    ("interface_localization_rate", "Interface localization", ".3f"),
    ("device_localization_rate", "Device localization", ".3f"),
    ("localization_composite_score", "Localization composite", ".3f"),
    ("overall_accuracy", "Overall accuracy", ".3f"),
    ("average_score", "Average score", ".3f"),
    ("avg_time_seconds", "Avg time (s)", ".1f"),
    ("avg_tool_calls", "Avg tool calls", ".1f"),
    ("total_input_tokens", "Total input tokens", "d"),
    ("avg_input_tokens_per_case", "Avg input tokens", ".1f"),
    ("total_output_tokens", "Total output tokens", "d"),
    ("avg_output_tokens_per_case", "Avg output tokens", ".1f"),
]


def _format_summary_block(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    rows: list[tuple[str, str]] = []
    for key, label, spec in _SUMMARY_LAYOUT:
        if key not in summary:
            continue
        rows.append((label, _fmt_number(summary.get(key), spec)))
    if not rows:
        return ""
    label_width = max(len(label) for label, _ in rows)
    value_width = max(len(value) for _, value in rows)
    lines = ["Summary", "-------"]
    for label, value in rows:
        lines.append(f"  {label.ljust(label_width)}  {value.rjust(value_width)}")
    return "\n".join(lines)


def _format_footer(artifact_paths: dict[str, str]) -> str:
    if not artifact_paths:
        return ""
    lines = ["Artifacts", "---------"]
    key_width = max(len(k) for k in artifact_paths)
    for key in sorted(artifact_paths):
        lines.append(f"  {key.ljust(key_width)}  {artifact_paths[key]}")
    return "\n".join(lines)


def _wrap_text(text: str, width: int, indent: str) -> str:
    """Wrap *text* so each line fits within *width* terminal columns.

    The first line is placed after a key prefix of ``len(indent)`` characters
    (via ``kv()``), so available content width is ``width - len(indent)`` for
    every line.  Continuation lines are prefixed with *indent*.
    """
    avail = max(10, width - len(indent))
    lines = textwrap.wrap(str(text or ""), width=avail)
    return ("\n" + indent).join(lines)


_MODE_LABELS: dict[str, str] = {
    "deterministic": "deterministic",
    "llm_judge": "llm judge",
    "judge_error": "judge error",
}
_KW = 26  # key column width for fault type judgment blocks


def _format_fault_type_judgments(detailed: list[dict[str, Any]]) -> str:
    """Format fault_type_judgment entries from detailed_results as a display section.

    Only cases that contain a ``fault_type_judgment`` dict are rendered.  When
    the agent type was resolved via an alias the resolved form is shown on a
    ``→ resolved to`` continuation line; ``canonical_ground_truth_fault_type``
    is omitted because scenario GT is always already canonical.
    """
    blocks: list[str] = []
    multi = sum(1 for item in detailed if (item.get("details") or {}).get("fault_type_judgment")) > 1

    case_idx = 0
    for item in detailed:
        details = item.get("details") or {}
        judgment = details.get("fault_type_judgment")
        if not judgment:
            continue
        case_idx += 1

        lines: list[str] = []
        if multi:
            sid = _short_scenario_id(item.get("scenario_id") or details.get("scenario_id") or "")
            lines.append(f"[Case {case_idx}: {sid}]")

        mode_raw = str(judgment.get("mode") or "deterministic")
        mode_label = _MODE_LABELS.get(mode_raw, mode_raw)

        def kv(key: str, value: Any) -> str:
            return f"  {key.ljust(_KW)}{value}"

        lines.append(kv("mode", mode_label))

        # Raw fault type strings live on agent_output / ground_truth inside
        # details.  The judgment dict omits them in llm_judge mode (it only
        # carries canonical forms), so we read from the parent case dicts.
        agent_output = details.get("agent_output") or {}
        ground_truth = details.get("ground_truth") or {}
        agent_raw = str(judgment.get("agent_fault_type") or agent_output.get("fault_type") or "-")
        gt_raw = str(judgment.get("ground_truth_fault_type") or ground_truth.get("fault_type") or "-")
        canonical_agent = str(judgment.get("canonical_agent_fault_type") or "")

        lines.append(kv("agent fault type", agent_raw))
        # Show resolved form only for the deterministic fast-path when the
        # alias table changed the value (space/case normalization + synonym
        # lookup).  In llm_judge mode the reasoning field covers this.
        normalized_raw = agent_raw.lower().replace(" ", "_")
        if mode_raw == "deterministic" and canonical_agent and canonical_agent not in {agent_raw, normalized_raw}:
            lines.append(kv("  \u2192 resolved to", canonical_agent))

        lines.append(kv("ground truth", gt_raw))
        lines.append(kv("match", _flag(judgment.get("is_match"))))

        if judgment.get("taxonomy_violation"):
            lines.append(kv("taxonomy violation", "Y"))

        if mode_raw == "llm_judge":
            lines.append(kv("confidence", _fmt_number(judgment.get("confidence"), ".2f")))
            if judgment.get("judge_model"):
                lines.append(kv("judge model", judgment["judge_model"]))
            reasoning = str(judgment.get("reasoning") or "")
            if reasoning:
                indent = " " * (_KW + 2)  # 2 leading spaces + key width
                wrapped = _wrap_text(reasoning, width=76, indent=indent)
                lines.append(kv("reasoning", wrapped))

        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    title = "Fault Type Judgments"
    sep = "-" * len(title)
    return "\n".join([title, sep] + blocks)


class RunHandle:
    """Thin handle for a public session run."""

    def __init__(
        self,
        *,
        id: str,
        mode: str,
        status: str,
        started_at: datetime,
        completed_at: datetime | None,
        artifact_dir: str,
        scenario_ids: list[str],
        runtime_id: str,
        report_path: Path,
    ):
        self.id = id
        self.mode = mode
        self.status = status
        self.started_at = started_at
        self.completed_at = completed_at
        self.artifact_dir = str(artifact_dir)
        self.scenario_ids = list(scenario_ids)
        self.runtime_id = runtime_id
        self.report_path = Path(report_path)

    def report(self) -> BenchmarkReport | None:
        if not self.report_path.exists():
            return None
        return BenchmarkReport.load(self.report_path)

    def wait(self, timeout: float | None = None, *, raise_on_failure: bool = False) -> BenchmarkReport:
        """Return the persisted :class:`BenchmarkReport` for this run.

        Because runs execute synchronously, ``wait()`` simply loads the report
        file written by the orchestrator. ``timeout`` is reserved for future
        use with async runs and currently ignored.

        Args:
            timeout: Reserved. Currently ignored.
            raise_on_failure: When True, raise :class:`RunFailedError` if the
                report indicates the run did not complete successfully
                (status != "completed", or any scenario marked failed).
                Defaults to False to preserve backward compatibility.

        Raises:
            FileNotFoundError: If no report file exists yet at
                :attr:`report_path`.
            RunFailedError: If ``raise_on_failure=True`` and the run failed.
        """
        report = self.report()
        if report is None:
            raise FileNotFoundError(f"report file not found: {self.report_path}")
        self.refresh()
        if raise_on_failure and not _report_succeeded(report):
            raise RunFailedError(
                f"benchmark run {self.id!r} did not complete successfully " f"(status={self.status!r})",
                report=report,
            )
        return report

    def refresh(self) -> RunHandle:
        report = self.report()
        if report is None:
            return self
        self.status = str(report.raw.get("status") or report.summary.get("status") or self.status)
        completed_value = report.raw.get("completed_at") or report.summary.get("completed_at")
        if completed_value is not None:
            self.completed_at = _coerce_datetime(completed_value)
        elif self.status in {"completed", "cancelled", "failed"} and self.completed_at is None:
            self.completed_at = self.started_at
        return self

    def cancel(self) -> None:
        if self.status not in {"completed", "failed", "cancelled"}:
            self.status = "cancelled"
            self.completed_at = self.completed_at or datetime.now(self.started_at.tzinfo)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _report_succeeded(report: BenchmarkReport) -> bool:
    """Return True if the report indicates a fully successful run."""
    status = str(report.raw.get("status") or report.summary.get("status") or "").lower()
    if status in {"failed", "cancelled", "error"}:
        return False
    if status and status != "completed":
        return False
    summaries = report.scenario_summaries or []
    for entry in summaries:
        entry_status = str(entry.get("status") or "").lower()
        if entry_status and entry_status not in {"completed", "passed", "ok"}:
            return False
    summary_success = report.summary.get("success")
    if summary_success is False:
        return False
    return True
