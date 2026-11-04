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
from netopsbench.evaluator.scorer import AgentOutput
from netopsbench.logging_utils import get_logger
from netopsbench.platform.incident.context import (
    extract_episode_pingmesh_query_window,
)
from netopsbench.platform.incident.engine import DiagnosticSession, SessionToolGateway
from netopsbench.platform.scenario.timing import timing_scope
from netopsbench.platform.session.trace_store import TraceWriter
from netopsbench.platform.session.types import WorkerExecutionContext
from netopsbench.platform.toolkit._core.control import diagnosis_budget_scope
from netopsbench.platform.utils.files import atomic_write_json

logger = get_logger(__name__)


def run_agent_diagnose(
    handle: Any, context: DiagnosticContext, cancel_event=None, *, timeout_seconds=None, timings=None
):
    """Execute a sync or async diagnosis handle from a synchronous session."""

    async def invoke():
        async def bounded():
            with timing_scope(timings):
                async with diagnosis_budget_scope(timeout_seconds):
                    return await handle.diagnose(context)

        task = asyncio.create_task(bounded())
        try:
            while not task.done():
                if cancel_event is not None and cancel_event.is_set():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise RuntimeError("diagnosis cancelled")
                await asyncio.wait({task}, timeout=0.2)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(invoke())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(invoke()))
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
    cancel_event=None,
):
    """Build the episode callback that presents observations to one agent."""
    handle = agent if isinstance(agent, AgentHandle) else AgentHandle(agent)
    context_dir = Path(topology_dir) / ".netopsbench"
    context_file = context_dir / "pingmesh_context.json"

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
        context = DiagnosticContext(
            scenario_id=case_id,
            topology=topology,
            symptoms=symptoms,
            tools=SessionToolGateway(diagnostic_session),
            trace=trace_recorder,
            metadata=metadata,
        )
        try:
            limit = diagnostic_session.config.max_agent_seconds
            diagnosis = run_agent_diagnose(
                handle,
                context,
                cancel_event,
                timeout_seconds=limit,
                timings=getattr(diagnostic_session, "phase_timings", None),
            )
        except Exception as exc:
            trace_recorder.record_error(stage="agent", error=exc)
            ended_at = datetime.now(UTC)
            failure_metadata: dict[str, Any] = {}
            cause: BaseException | None = exc
            while cause is not None:
                failure_metadata.update(getattr(cause, "agent_metadata", {}))
                cause = cause.__cause__
            diagnosis_payload: dict[str, Any] = {
                "error": str(exc),
                "success": False,
                "time_taken_seconds": max(0.0, (ended_at - start_time).total_seconds()),
                "metadata": {**failure_metadata, "agent_failure_stage": "diagnose", "error_type": type(exc).__name__},
            }
            diagnosis = SimpleNamespace(
                agent_name=getattr(handle, "name", "agent"),
                success=False,
                findings={"error": str(exc)},
                metadata=diagnosis_payload["metadata"],
            )
        else:
            ended_at = datetime.now(UTC)
            output = AgentOutput.from_diagnosis(
                diagnosis,
                merge_location_fields=True,
                metadata_evidence=False,
                findings_tool_calls=False,
                time_taken_seconds=max(0.0, (ended_at - start_time).total_seconds()),
            )
            recorder_metrics = trace_recorder.metrics()
            for key in ("input_tokens", "output_tokens", "total_tokens", "llm_call_count"):
                if recorder_metrics.get(key):
                    output.metadata[key] = recorder_metrics[key]
            output.tool_calls = trace_recorder.tool_calls() or output.tool_calls
            diagnosis_payload = {"success": diagnosis.success, **vars(output)}
            if not diagnosis.success:
                diagnosis_payload["error"] = diagnosis.reasoning or "agent diagnosis failed"
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
                    error=diagnosis_payload.get("error"),
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
