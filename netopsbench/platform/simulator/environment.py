"""Public reset/step facade over the shared incident execution engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from netopsbench.platform.simulator.contracts import (
    AgentUsage,
    DiagnosisLocation,
    DiagnosisSubmission,
    SimulatorConfig,
    SubmitDiagnosisAction,
    ToolAction,
)
from netopsbench.platform.simulator.engine import (
    CleanupStatus,
    DiagnosticSession,
    ExecutionFailure,
    FailureDomain,
    IncidentEngine,
    IncidentState,
    PreparedIncident,
    SessionState,
    TerminationReason,
)


class ResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    case_valid: bool
    state: IncidentState
    case_id: str
    observation: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    failure: ExecutionFailure | None = None
    cleanup_status: CleanupStatus = CleanupStatus.NOT_STARTED
    error: str | None = None


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    case_valid: bool
    state: SessionState
    observation: dict[str, Any] = Field(default_factory=dict)
    reward: float | None = None
    reward_components: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    termination_reason: TerminationReason | None = None
    failure: ExecutionFailure | None = None
    cleanup_status: CleanupStatus = CleanupStatus.NOT_STARTED
    error: str | None = None


class DiagnosticEnvironment:
    """One-agent reset/step view over :class:`PreparedIncident`."""

    def __init__(
        self,
        engine: IncidentEngine,
        scenario: Any,
        config: SimulatorConfig,
        on_close: Callable[[DiagnosticEnvironment], None] | None = None,
    ):
        self.engine = engine
        self.scenario = scenario
        self.config = config
        self.incident: PreparedIncident | None = None
        self.session: DiagnosticSession | None = None
        self._on_close = on_close
        self._close_notified = False

    @property
    def state(self) -> IncidentState:
        if self.incident is None:
            return IncidentState.CREATED
        return self.incident.state

    def reset(self) -> ResetResult:
        if self.incident is not None:
            raise RuntimeError("Diagnostic environments are single-use; create a new environment")
        self.incident = self.engine.prepare(self.scenario)
        if self.incident.state is IncidentState.BROKEN:
            failure = self.incident.failure
            self._notify_close()
            return ResetResult(
                valid=False,
                case_valid=False,
                state=self.incident.state,
                case_id=self.incident.case_id,
                failure=failure,
                cleanup_status=self.incident.cleanup_status,
                error=failure.message if failure else "incident preparation failed",
            )
        self.session = self.incident.open_session(self.config)
        return ResetResult(
            valid=True,
            case_valid=True,
            state=self.incident.state,
            case_id=self.incident.case_id,
            observation=self.incident.observation,
            tools=self.incident.tool_schemas,
            cleanup_status=self.incident.cleanup_status,
        )

    def step(self, action: ToolAction | SubmitDiagnosisAction) -> StepResult:
        if self.session is None:
            raise RuntimeError("Environment must be reset before step")
        if isinstance(action, ToolAction):
            transition = self.session.call_tool(action)
        else:
            transition = self.session.submit(action.diagnosis, usage=action.usage)
            cleanup = self.incident.close() if self.incident is not None else CleanupStatus.NOT_STARTED
            cleanup_failure = (
                self.incident.failure
                if self.incident is not None and cleanup is CleanupStatus.FAILED
                else None
            )
            transition = transition.model_copy(
                update={
                    "cleanup_status": cleanup,
                    "failure": cleanup_failure,
                    "error": cleanup_failure.message if cleanup_failure is not None else None,
                }
            )
            self._notify_close()
        return StepResult.model_validate(transition.model_dump(mode="python"))

    def terminate_protocol(self, message: str) -> StepResult:
        """Terminate an otherwise valid case after an invalid action envelope."""
        if self.session is None:
            raise RuntimeError("Environment must be reset before protocol termination")
        transition = self.session.terminate_failure(
            domain=FailureDomain.PROTOCOL,
            message=message,
            reason=TerminationReason.PROTOCOL_ERROR,
        )
        cleanup = self.incident.close() if self.incident is not None else CleanupStatus.NOT_STARTED
        cleanup_failure = (
            self.incident.failure
            if self.incident is not None and cleanup is CleanupStatus.FAILED
            else None
        )
        if cleanup_failure is not None:
            transition = transition.model_copy(
                update={
                    "cleanup_status": cleanup,
                    "failure": cleanup_failure,
                    "error": cleanup_failure.message,
                }
            )
        else:
            transition = transition.model_copy(update={"cleanup_status": cleanup})
        self._notify_close()
        return StepResult.model_validate(transition.model_dump(mode="python"))

    def close(self) -> None:
        if self.incident is not None:
            self.incident.close()
        self._notify_close()

    def _notify_close(self) -> None:
        if not self._close_notified and self._on_close is not None:
            self._close_notified = True
            self._on_close(self)

__all__ = [
    "AgentUsage",
    "CleanupStatus",
    "DiagnosisLocation",
    "DiagnosisSubmission",
    "DiagnosticEnvironment",
    "ExecutionFailure",
    "FailureDomain",
    "ResetResult",
    "SimulatorConfig",
    "StepResult",
    "SubmitDiagnosisAction",
    "TerminationReason",
    "ToolAction",
]
