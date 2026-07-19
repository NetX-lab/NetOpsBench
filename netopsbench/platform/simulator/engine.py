"""Single incident lifecycle and diagnosis semantics for benchmark and simulator."""

from __future__ import annotations

import threading
import time
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from netopsbench.evaluator.scorer import AgentOutput, Evaluator
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.session.scoring import build_episode_ground_truth
from netopsbench.platform.simulator.contracts import (
    AgentUsage,
    DiagnosisSubmission,
    SimulatorConfig,
    ToolAction,
)
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp.registry import tool_schemas, validate_tool_call


class IncidentState(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    BROKEN = "broken"


class SessionState(StrEnum):
    ACTIVE = "active"
    TERMINAL = "terminal"
    BROKEN = "broken"


class FailureDomain(StrEnum):
    INFRASTRUCTURE = "infrastructure"
    AGENT = "agent"
    PROTOCOL = "protocol"
    TOOL_EXECUTION = "tool_execution"
    CLEANUP = "cleanup"


class CleanupStatus(StrEnum):
    NOT_STARTED = "not_started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TerminationReason(StrEnum):
    SUBMITTED = "submitted"
    AGENT_ERROR = "agent_error"
    PROTOCOL_ERROR = "protocol_error"
    LIMIT_EXHAUSTED = "limit_exhausted"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    CANCELLED = "cancelled"


class ExecutionFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: FailureDomain
    phase: str
    message: str
    error_type: str | None = None


class SessionTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool = True
    case_valid: bool = True
    state: SessionState
    observation: dict[str, Any] = Field(default_factory=dict)
    reward: float | None = None
    reward_components: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    termination_reason: TerminationReason | None = None
    failure: ExecutionFailure | None = None
    cleanup_status: CleanupStatus = CleanupStatus.NOT_STARTED
    error: str | None = None


class IncidentBackend(Protocol):
    topology_dir: str | None

    def prepare(self, scenario: ScenarioSpec) -> dict[str, Any]: ...

    def call_tool(self, action: ToolAction) -> dict[str, Any]: ...

    def finish(self, *, broken: bool = False) -> None: ...

    def close(self) -> None: ...


class IncidentEngine:
    """Prepare physical incidents through one authoritative state machine."""

    def __init__(self, backend_factory: Any, evaluator: Evaluator | None = None):
        self._backend_factory = backend_factory
        self._evaluator = evaluator or Evaluator()

    def prepare(self, scenario: ScenarioSpec) -> PreparedIncident:
        backend = self._backend_factory()
        incident = PreparedIncident(scenario=scenario, backend=backend, evaluator=self._evaluator)
        incident.prepare()
        return incident


class PreparedIncident:
    """A physical incident that can host one or more diagnostic sessions."""

    def __init__(self, *, scenario: ScenarioSpec, backend: IncidentBackend, evaluator: Evaluator):
        self.scenario = scenario
        self.backend = backend
        self.evaluator = evaluator
        self.state = IncidentState.CREATED
        self.observation: dict[str, Any] = {}
        self.tool_schemas = tool_schemas()
        self.failure: ExecutionFailure | None = None
        self.cleanup_status = CleanupStatus.NOT_STARTED
        self._sessions: list[DiagnosticSession] = []
        self._tool_lock = threading.Lock()
        self._close_lock = threading.Lock()

    @property
    def case_id(self) -> str:
        value = self.observation.get("case_id")
        return str(value) if value else f"case-{self.scenario.digest[:12]}"

    def prepare(self) -> None:
        if self.state is not IncidentState.CREATED:
            raise RuntimeError(f"Incident cannot prepare from {self.state}")
        self.state = IncidentState.PREPARING
        try:
            self.observation = self.backend.prepare(self.scenario)
        except Exception as exc:  # noqa: BLE001 - normalize infrastructure failures
            self.failure = ExecutionFailure(
                domain=FailureDomain.INFRASTRUCTURE,
                phase="prepare",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            self.state = IncidentState.BROKEN
            self._finish_backend(broken=True)
            return
        self.state = IncidentState.ACTIVE

    def open_session(self, config: SimulatorConfig) -> DiagnosticSession:
        if self.state is not IncidentState.ACTIVE:
            raise RuntimeError(f"Incident is not active: {self.state}")
        session = DiagnosticSession(self, config)
        self._sessions.append(session)
        return session

    def call_tool(self, action: ToolAction) -> dict[str, Any]:
        if self.state is not IncidentState.ACTIVE:
            raise RuntimeError(f"Incident is not active: {self.state}")
        with self._tool_lock:
            return self.backend.call_tool(action)

    def evaluate(self, diagnosis: DiagnosisSubmission) -> tuple[float, dict[str, Any], dict[str, Any]]:
        episode = self.scenario.episode
        ground_truth = (
            {}
            if episode.is_healthy
            else build_episode_ground_truth(
                episode.model_dump(mode="json"),
                topology_dir=self.backend.topology_dir,
            )
        )
        evaluation = self.evaluator.evaluate(
            AgentOutput(
                verdict=diagnosis.verdict,
                fault_type=diagnosis.fault_type,
                location=diagnosis.location.model_dump(exclude_none=True),
                evidence=diagnosis.evidence,
                confidence=diagnosis.confidence,
                reasoning=diagnosis.reasoning,
            ),
            ground_truth,
            self.case_id,
        )
        return float(evaluation.score), {
            "correct_verdict": evaluation.correct_verdict,
            "correct_device": evaluation.correct_device,
            "correct_interface": evaluation.correct_interface,
            "correct_fault_type": evaluation.correct_fault_type,
            "fault_type_kpi": 1.0 if evaluation.correct_fault_type else 0.0,
        }, evaluation.to_dict()

    def close(self) -> CleanupStatus:
        with self._close_lock:
            if self.state is IncidentState.CLOSED:
                return self.cleanup_status
            if self.cleanup_status is not CleanupStatus.NOT_STARTED:
                return self.cleanup_status
            broken = self.state is IncidentState.BROKEN
            if not broken:
                self.state = IncidentState.CLOSING
            self._finish_backend(broken=broken)
            return self.cleanup_status

    def _finish_backend(self, *, broken: bool) -> None:
        try:
            self.backend.finish(broken=broken)
        except Exception as exc:  # noqa: BLE001 - cleanup is reported separately
            self.cleanup_status = CleanupStatus.FAILED
            cleanup_failure = ExecutionFailure(
                domain=FailureDomain.CLEANUP,
                phase="cleanup",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            if self.failure is None or self.failure.domain is FailureDomain.CLEANUP:
                self.failure = cleanup_failure
            self.state = IncidentState.BROKEN
            return
        self.cleanup_status = CleanupStatus.SUCCEEDED
        self.state = IncidentState.BROKEN if broken else IncidentState.CLOSED


class DiagnosticSession:
    """Per-agent counters and evaluation over one prepared incident."""

    def __init__(self, incident: PreparedIncident, config: SimulatorConfig):
        self.incident = incident
        self.config = config
        self.state = SessionState.ACTIVE
        self.started_at = time.monotonic()
        self.tool_calls = 0
        self.evaluation_result: dict[str, Any] | None = None

    @property
    def observation(self) -> dict[str, Any]:
        return self.incident.observation

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.incident.tool_schemas

    def call_tool(self, action: ToolAction) -> SessionTransition:
        self._require_active()
        elapsed = self._elapsed()
        limit = self._limit_transition(elapsed)
        if limit is not None:
            return limit
        self.tool_calls += 1
        if not validate_tool_call(action.name, action.arguments):
            failure = ExecutionFailure(
                domain=FailureDomain.PROTOCOL,
                phase="tool_validation",
                message=f"Invalid tool action: {action.name}",
            )
            return SessionTransition(
                state=self.state,
                observation={"tool": action.name, "success": False, "error": failure.message},
                failure=failure,
                error=failure.message,
                metrics=self._metrics(elapsed),
            )
        try:
            result = self.incident.call_tool(action)
        except Exception as exc:  # noqa: BLE001 - ordinary query errors are recoverable
            failure = ExecutionFailure(
                domain=FailureDomain.TOOL_EXECUTION,
                phase="tool_execution",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            return SessionTransition(
                state=self.state,
                observation={"tool": action.name, "success": False, "error": str(exc)},
                failure=failure,
                error=str(exc),
                metrics=self._metrics(elapsed),
            )
        return SessionTransition(
            state=self.state,
            observation={"tool": action.name, "result": result},
            metrics=self._metrics(elapsed),
        )

    def submit(self, diagnosis: DiagnosisSubmission, *, usage: AgentUsage | None = None) -> SessionTransition:
        self._require_active()
        elapsed = self._elapsed()
        limit = self._limit_transition(elapsed)
        if limit is not None:
            return limit
        reward, evaluation_metrics, self.evaluation_result = self.incident.evaluate(diagnosis)
        self.state = SessionState.TERMINAL
        metrics = {**evaluation_metrics, **self._metrics(elapsed)}
        if usage is not None:
            metrics.update(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            )
        return SessionTransition(
            state=self.state,
            reward=reward,
            reward_components={"outcome": reward},
            metrics=metrics,
            termination_reason=TerminationReason.SUBMITTED,
        )

    def terminate_failure(
        self,
        *,
        domain: FailureDomain,
        message: str,
        reason: TerminationReason,
    ) -> SessionTransition:
        self._require_active()
        self.state = SessionState.TERMINAL
        failure = ExecutionFailure(domain=domain, phase="diagnosis", message=message)
        return SessionTransition(
            state=self.state,
            reward=0.0,
            reward_components={"outcome": 0.0},
            metrics=self._metrics(self._elapsed()),
            termination_reason=reason,
            failure=failure,
            error=message,
        )

    def _limit_transition(self, elapsed: float) -> SessionTransition | None:
        message = None
        if elapsed > self.config.max_agent_seconds:
            message = "max_agent_seconds exceeded"
        elif self.tool_calls >= self.config.max_tool_calls:
            message = "max_tool_calls exceeded"
        if message is None:
            return None
        self.state = SessionState.TERMINAL
        failure = ExecutionFailure(domain=FailureDomain.AGENT, phase="limits", message=message)
        return SessionTransition(
            state=self.state,
            reward=0.0,
            reward_components={"outcome": 0.0},
            metrics=self._metrics(elapsed),
            termination_reason=TerminationReason.LIMIT_EXHAUSTED,
            failure=failure,
            error=message,
        )

    def _metrics(self, elapsed: float) -> dict[str, Any]:
        return {"tool_calls": self.tool_calls, "elapsed_seconds": elapsed}

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def _require_active(self) -> None:
        if self.state is not SessionState.ACTIVE:
            raise RuntimeError(f"Diagnostic session is not active: {self.state}")


class SessionToolGateway:
    """AgentToolkit-compatible view that routes calls through a session."""

    def __init__(self, session: DiagnosticSession):
        self.session = session

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**arguments: Any) -> ToolResult:
            transition = self.session.call_tool(ToolAction(name=name, arguments=arguments))
            if transition.failure is not None:
                return ToolResult(success=False, data=None, error=transition.failure.message)
            payload = transition.observation.get("result")
            if isinstance(payload, dict) and "success" in payload and "data" in payload:
                return ToolResult(
                    success=bool(payload.get("success")),
                    data=payload.get("data"),
                    error=payload.get("error"),
                )
            return ToolResult(success=True, data=payload)

        return call


__all__ = [
    "CleanupStatus",
    "DiagnosticSession",
    "ExecutionFailure",
    "FailureDomain",
    "IncidentBackend",
    "IncidentEngine",
    "IncidentState",
    "PreparedIncident",
    "SessionState",
    "SessionToolGateway",
    "SessionTransition",
    "TerminationReason",
]
