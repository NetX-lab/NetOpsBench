"""Optional localhost HTTP facade for the generic simulator."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.platform.session.context import build_public_case_id
from netopsbench.platform.simulator.contracts import (
    SimulatorConfig,
    SubmitDiagnosisAction,
    ToolAction,
)

Action = Annotated[ToolAction | SubmitDiagnosisAction, Field(discriminator="type")]
_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)
logger = logging.getLogger(__name__)


def scenario_case_id(scenario: ScenarioSpec) -> str:
    return build_public_case_id(
        scenario_id=scenario.id,
        episode_result={"episode": {"episode_id": scenario.episode.episode_id}},
    )


class CreateEnvironmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: dict[str, Any]


@dataclass
class _EnvironmentSlot:
    environment: Any
    case_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)


class SimulatorService:
    def __init__(
        self,
        manager: Any,
        scenarios: list[ScenarioSpec] | tuple[ScenarioSpec, ...],
        config: SimulatorConfig | None = None,
        *,
        event_log: Path | None = None,
    ):
        self.manager = manager
        self.config = config or SimulatorConfig()
        self.scenarios = {scenario_case_id(scenario): scenario for scenario in scenarios}
        if len(self.scenarios) != len(scenarios):
            raise ValueError("Simulator scenarios must produce unique opaque case ids")
        self.environments: dict[str, _EnvironmentSlot] = {}
        self._lock = threading.RLock()
        self._event_lock = threading.Lock()
        self._event_log = event_log
        if event_log is not None:
            event_log.parent.mkdir(parents=True, exist_ok=True)

    def list_cases(self) -> list[str]:
        return sorted(self.scenarios)

    def create(self, case_id: str) -> dict[str, Any]:
        scenario = self.scenarios.get(case_id)
        if scenario is None:
            raise KeyError(case_id)
        environment = self.manager.create(scenario=scenario, config=self.config)
        environment_id = uuid.uuid4().hex
        result = environment.reset().model_dump(mode="json")
        with self._lock:
            self.environments[environment_id] = _EnvironmentSlot(environment, case_id)
        self._record_event(
            {
                "event": "create",
                "environment_id": environment_id,
                "case_id": case_id,
                "valid": result.get("valid"),
                "state": result.get("state"),
                "error": result.get("error"),
            }
        )
        return {"environment_id": environment_id, "reset": result}

    def step(self, environment_id: str, action: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            slot = self.environments.get(environment_id)
        if slot is None:
            raise KeyError(environment_id)
        with slot.lock:
            try:
                parsed = _ACTION_ADAPTER.validate_python(action)
            except ValidationError as exc:
                result = slot.environment.terminate_protocol(str(exc)).model_dump(mode="json")
            else:
                result = slot.environment.step(parsed).model_dump(mode="json")
        self._record_event(
            {
                "event": "step",
                "environment_id": environment_id,
                "case_id": slot.case_id,
                "action_type": action.get("type"),
                "result": result,
            }
        )
        return result

    def delete(self, environment_id: str) -> None:
        with self._lock:
            slot = self.environments.pop(environment_id, None)
        if slot is None:
            raise KeyError(environment_id)
        with slot.lock:
            slot.environment.close()
        self._record_event(
            {"event": "delete", "environment_id": environment_id, "case_id": slot.case_id}
        )

    def close(self) -> None:
        with self._lock:
            slots = list(self.environments.values())
            self.environments.clear()
        for slot in slots:
            try:
                with slot.lock:
                    slot.environment.close()
            except Exception:
                logger.warning("Failed to close simulator environment", exc_info=True)
        self.manager.close()

    def _record_event(self, event: dict[str, Any]) -> None:
        if self._event_log is None:
            return
        payload = {"timestamp": datetime.now(UTC).isoformat(), **event}
        with self._event_lock, self._event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def create_app(service: SimulatorService):
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise RuntimeError("Install NetOpsBench with the 'simulator' extra to run the service") from exc

    @asynccontextmanager
    async def lifespan(_app):
        yield
        service.close()

    app = FastAPI(title="NetOpsBench Simulator", version="1", lifespan=lifespan)

    @app.get("/v1/cases")
    def list_cases():
        return {"case_ids": service.list_cases()}

    @app.post("/v1/environments")
    def create_environment(request: CreateEnvironmentRequest):
        try:
            return service.create(request.case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown simulator case") from exc

    @app.post("/v1/environments/{environment_id}/actions")
    def step_environment(environment_id: str, request: StepRequest):
        try:
            return service.step(environment_id, request.action)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown simulator environment") from exc

    @app.delete("/v1/environments/{environment_id}")
    def delete_environment(environment_id: str):
        try:
            service.delete(environment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown simulator environment") from exc
        return {"deleted": True}

    return app


__all__ = [
    "CreateEnvironmentRequest",
    "SimulatorService",
    "StepRequest",
    "create_app",
    "scenario_case_id",
]
