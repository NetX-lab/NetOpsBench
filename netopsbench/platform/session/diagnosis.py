"""Diagnostic callback helpers for runtime-backed session execution."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.handle import AgentHandle
from netopsbench.agents.tracing import AgentTraceRecorder
from netopsbench.logging_utils import get_logger
from netopsbench.platform.incident.context import (
    extract_episode_pingmesh_query_window,
)
from netopsbench.platform.incident.engine import DiagnosticSession, SessionToolGateway
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.session.types import WorkerExecutionContext
from netopsbench.platform.utils.files import atomic_write_json

logger = get_logger(__name__)


def run_agent_diagnose(handle: Any, context: DiagnosticContext):
    """Execute a sync or async diagnosis handle from a synchronous session."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(handle.diagnose(context))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(handle.diagnose(context)))
        return future.result()


def _strip_runtime_trace_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(metadata or {})
    for key in ("messages", "raw_messages", "trace_events", "trace", "trajectory", "conversation"):
        cleaned.pop(key, None)
    return cleaned


def build_runtime_diagnosis_callback(
    agent: Any,
    topology_dir: str,
    scenario_id: str,
    worker_context: WorkerExecutionContext | None = None,
    trace_writer: TraceWriter | None = None,
    worker_name: str | None = None,
    runtime_id: str | None = None,
    scenario_scale: str | None = None,
):
    """Build the episode callback that presents observations to one agent."""
    handle = agent if isinstance(agent, AgentHandle) else AgentHandle(agent)
    context_dir = Path(topology_dir) / ".netopsbench"
    context_file = context_dir / "pingmesh_context.json"
    worker_env = worker_context.as_env() if worker_context is not None else {}
    worker_env["NETOPSBENCH_PINGMESH_CONTEXT_FILE"] = str(context_file)

    def callback(
        episode_result: dict,
        *,
        diagnostic_session: DiagnosticSession,
        diagnostic_payload: dict[str, Any],
    ) -> dict:
        start_time = datetime.now(UTC)
        trace_recorder = AgentTraceRecorder(enabled=trace_writer is not None)
        pingmesh_query_window = extract_episode_pingmesh_query_window(episode_result)
        window_start = pingmesh_query_window.get("start_time")
        window_end = pingmesh_query_window.get("end_time")
        context_payload = {"start_time": window_start, "end_time": window_end} if window_start and window_end else {}
        atomic_write_json(context_file, context_payload)

        case_id = str(diagnostic_payload["case_id"])
        topology = diagnostic_payload["topology"]
        symptoms = diagnostic_payload["symptoms"]
        canonical_observation = diagnostic_payload["canonical_observation"]
        metadata: dict[str, Any] = {"canonical_observation": canonical_observation}
        if worker_env:
            metadata["worker_env"] = worker_env
        context = DiagnosticContext(
            scenario_id=case_id,
            topology=topology,
            symptoms=symptoms,
            tools=SessionToolGateway(diagnostic_session),
            trace=trace_recorder,
            metadata=metadata,
        )
        try:
            diagnosis = run_agent_diagnose(handle, context)
        except Exception as exc:
            trace_recorder.record_error(stage="agent", error=exc)
            ended_at = datetime.now(UTC)
            diagnosis_payload: dict[str, Any] = {
                "error": str(exc),
                "success": False,
                "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
                "metadata": {"agent_failure_stage": "diagnose", "error_type": type(exc).__name__},
            }
            if trace_writer is not None:
                try:
                    trace_result = trace_writer.write_case_trace(
                        case_id=context.scenario_id,
                        scenario_id=scenario_id,
                        episode_result=episode_result,
                        worker=worker_name or "worker",
                        topology_id=(worker_context.topology_id if worker_context is not None else None),
                        topology_scale=scenario_scale,
                        runtime_id=runtime_id or "",
                        agent=agent,
                        diagnostic_context=context,
                        diagnosis=SimpleNamespace(
                            agent_name=getattr(handle, "name", "agent"),
                            success=False,
                            findings={"error": str(exc)},
                            metadata=diagnosis_payload["metadata"],
                        ),
                        diagnosis_payload=diagnosis_payload,
                        started_at=start_time,
                        ended_at=ended_at,
                        pingmesh_window=pingmesh_query_window,
                        error=str(exc),
                        trace_recorder=trace_recorder,
                    )
                    diagnosis_payload["trace"] = {
                        "trace_id": trace_result.trace_id,
                        "case_id": trace_result.case_id,
                        "worker": trace_result.worker,
                        "atif_path": trace_result.atif_path,
                    }
                except Exception:
                    logger.debug("failed to persist failed agent runtime trace", exc_info=True)
            diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
            return diagnosis_payload

        findings = dict(diagnosis.findings or {})
        location = findings.get("location") or {}
        if not isinstance(location, dict):
            location = {}
        ended_at = datetime.now(UTC)
        metadata = dict(diagnosis.metadata or {})
        recorder_metrics = trace_recorder.metrics()
        for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
            if recorder_metrics.get(key):
                metadata[key] = recorder_metrics[key]
        recorded_tool_calls = trace_recorder.tool_calls()
        diagnosis_payload = {
            "verdict": diagnosis.verdict,
            "fault_type": findings.get("fault_type") or metadata.get("fault_type"),
            "location": {
                key: value
                for key, value in {
                    "device": location.get("device") or findings.get("device"),
                    "interface": location.get("interface") or findings.get("interface"),
                }.items()
                if value is not None
            },
            "evidence": list(findings.get("evidence") or []),
            "confidence": float(diagnosis.confidence or 0.0),
            "reasoning": diagnosis.reasoning,
            "tool_calls": recorded_tool_calls or list(metadata.get("tool_calls") or []),
            "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
            "metadata": metadata,
        }
        if trace_writer is not None:
            try:
                trace_result = trace_writer.write_case_trace(
                    case_id=context.scenario_id,
                    scenario_id=scenario_id,
                    episode_result=episode_result,
                    worker=worker_name or "worker",
                    topology_id=(worker_context.topology_id if worker_context is not None else None),
                    topology_scale=scenario_scale,
                    runtime_id=runtime_id or "",
                    agent=agent,
                    diagnostic_context=context,
                    diagnosis=diagnosis,
                    diagnosis_payload=diagnosis_payload,
                    started_at=start_time,
                    ended_at=ended_at,
                    pingmesh_window=pingmesh_query_window,
                    trace_recorder=trace_recorder,
                )
                diagnosis_payload["trace"] = {
                    "trace_id": trace_result.trace_id,
                    "case_id": trace_result.case_id,
                    "worker": trace_result.worker,
                    "atif_path": trace_result.atif_path,
                }
            except Exception:
                logger.debug("failed to persist agent runtime trace", exc_info=True)
        diagnosis_payload["metadata"] = _strip_runtime_trace_metadata(diagnosis_payload["metadata"])
        return diagnosis_payload

    return callback


__all__ = ["build_runtime_diagnosis_callback", "run_agent_diagnose"]
