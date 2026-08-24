"""Runtime helpers for the minimal_deepagent example."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.sessions import create_session
from langchain_mcp_adapters.tools import load_mcp_tools


class MCPHealthStatus(StrEnum):
    """Health of one case-scoped MCP client generation."""

    HEALTHY = "healthy"
    CLIENT_CLOSED = "client_closed"
    SERVER_UNAVAILABLE = "server_unavailable"
    INITIALIZATION_FAILED = "initialization_failed"
    HEALTH_CHECK_TIMEOUT = "health_check_timeout"


class MCPInfrastructureError(RuntimeError):
    """A bounded MCP recovery attempt could not restore the case transport."""


_TRANSPORT_ERROR_MARKERS = (
    "connection closed",
    "transport closed",
    "stream closed",
    "session unavailable",
    "broken pipe",
    "end of stream",
    "endofstream",
    "eof",
    "closedresourceerror",
    "brokenresourceerror",
)

# These tools send packets. A transport failure makes delivery ambiguous, so
# automatically replaying the call could exceed the harness packet budget.
_NON_RETRYABLE_ACTIVE_TOOLS = frozenset(
    {
        "ping_test",
        "ping_link_test",
        "traceroute",
        "payload_integrity_test",
        "payload_integrity_link_test",
    }
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fd_count() -> int | None:
    try:
        return len(tuple(Path("/proc/self/fd").iterdir()))
    except OSError:
        return None


def _pending_task_count() -> int:
    try:
        current = asyncio.current_task()
        return sum(1 for task in asyncio.all_tasks() if task is not current and not task.done())
    except RuntimeError:
        return 0


def is_mcp_transport_error(exc: BaseException) -> bool:
    """Recognize transport/session failures without classifying tool errors."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSPORT_ERROR_MARKERS)


@dataclass
class MCPLifecycleMetrics:
    """Per-case counters persisted into the diagnosis metadata."""

    mcp_client_created: int = 0
    mcp_client_closed: int = 0
    mcp_session_created: int = 0
    mcp_session_closed: int = 0
    mcp_reconnect_attempts: int = 0
    mcp_reconnect_successes: int = 0
    mcp_reconnect_failures: int = 0
    mcp_health_check_failures: int = 0
    mcp_midcase_disconnects: int = 0
    mcp_live_clients_after_case: int = 0
    mcp_pending_tasks_after_case: int = 0
    fd_count_before_case: int | None = None
    fd_count_after_case: int | None = None


@dataclass
class MCPLifecycleTrace:
    """Sanitized lifecycle events for one diagnosis case."""

    case_id: str
    metrics: MCPLifecycleMetrics = field(default_factory=MCPLifecycleMetrics)
    events: list[dict[str, Any]] = field(default_factory=list)
    infrastructure_degraded: bool = False
    recovery_exhausted: bool = False
    failure_stage: str | None = None

    def __post_init__(self) -> None:
        self.metrics.fd_count_before_case = _fd_count()
        self._tasks_before = _pending_task_count()

    def record(
        self,
        event: str,
        *,
        server_name: str,
        session_id: str | None = None,
        status: MCPHealthStatus | None = None,
        stage: str | None = None,
        error: BaseException | None = None,
        **details: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "timestamp": _utc_now(),
            "server_name": server_name,
        }
        if session_id:
            payload["session_id"] = session_id
        if status is not None:
            payload["status"] = status.value
        if stage:
            payload["stage"] = stage
        if error is not None:
            # Do not persist exception messages: transports and provider
            # libraries may include URLs, headers, or environment details.
            payload["error_type"] = type(error).__name__
            payload["error_class"] = "transport" if is_mcp_transport_error(error) else "initialization"
        payload.update(details)
        self.events.append(payload)

    def finalize(self) -> None:
        self.metrics.fd_count_after_case = _fd_count()
        self.metrics.mcp_live_clients_after_case = max(
            0,
            self.metrics.mcp_client_created - self.metrics.mcp_client_closed,
        )
        self.metrics.mcp_pending_tasks_after_case = max(0, _pending_task_count() - self._tasks_before)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "infrastructure_degraded": self.infrastructure_degraded,
            "recovery_exhausted": self.recovery_exhausted,
            "failure_stage": self.failure_stage,
            "metrics": vars(self.metrics).copy(),
            "events": list(self.events),
        }


class _ManagedMCPConnection:
    """One server connection whose stale generations are never reused."""

    def __init__(
        self,
        name: str,
        connection: dict[str, Any],
        lifecycle: MCPLifecycleTrace,
        *,
        health_timeout_seconds: float,
        tool_name_prefix: bool,
        recreate_allowed,
        session_factory=create_session,
        tool_loader=load_mcp_tools,
    ) -> None:
        self.name = name
        self.connection = connection
        self.lifecycle = lifecycle
        self.health_timeout_seconds = health_timeout_seconds
        self.tool_name_prefix = tool_name_prefix
        self._recreate_allowed = recreate_allowed
        self._session_factory = session_factory
        self._tool_loader = tool_loader
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._session_id: str | None = None
        self._generation = 0
        self._healthy = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def open(self) -> list[Any]:
        try:
            return await self._open_generation(reconnect=False)
        except Exception as exc:
            self.lifecycle.infrastructure_degraded = True
            self.lifecycle.metrics.mcp_health_check_failures += 1
            self.lifecycle.failure_stage = self.lifecycle.failure_stage or "pre_case_health_check"
            if not self._recreate_allowed():
                self.lifecycle.recovery_exhausted = True
                raise MCPInfrastructureError("MCP pre-case health check failed") from exc
            self.lifecycle.metrics.mcp_reconnect_attempts += 1
            self.lifecycle.record(
                "reconnect_attempt",
                server_name=self.name,
                session_id=self._session_id,
                stage="pre_case_health_check",
                error=exc,
            )
            await self.close_generation()
            try:
                tools = await self._open_generation(reconnect=True)
            except Exception as retry_exc:
                self.lifecycle.metrics.mcp_reconnect_failures += 1
                self.lifecycle.metrics.mcp_health_check_failures += 1
                self.lifecycle.recovery_exhausted = True
                self.lifecycle.failure_stage = "pre_case_recreate"
                self.lifecycle.record(
                    "reconnect_failed",
                    server_name=self.name,
                    session_id=self._session_id,
                    stage="pre_case_recreate",
                    error=retry_exc,
                )
                raise MCPInfrastructureError("MCP pre-case recreate failed") from retry_exc
            self.lifecycle.metrics.mcp_reconnect_successes += 1
            self.lifecycle.record(
                "reconnect_succeeded",
                server_name=self.name,
                session_id=self._session_id,
                stage="pre_case_recreate",
            )
            return tools

    async def _open_generation(self, *, reconnect: bool) -> list[Any]:
        self._generation += 1
        generation = self._generation
        self._session_id = f"{self.lifecycle.case_id}:mcp:{self.name}:{uuid4().hex[:12]}"
        self._healthy = False
        stack = AsyncExitStack()
        await stack.__aenter__()
        self._stack = stack
        self.lifecycle.metrics.mcp_client_created += 1
        self.lifecycle.record(
            "client_created",
            server_name=self.name,
            session_id=self._session_id,
            generation=generation,
            reconnect=reconnect,
        )
        try:
            session = await asyncio.wait_for(
                stack.enter_async_context(self._session_factory(self.connection)),
                timeout=self.health_timeout_seconds,
            )
            self._session = session
            self.lifecycle.metrics.mcp_session_created += 1
            self.lifecycle.record(
                "session_created",
                server_name=self.name,
                session_id=self._session_id,
                generation=generation,
            )
            try:
                await asyncio.wait_for(session.initialize(), timeout=self.health_timeout_seconds)
            except TimeoutError as exc:
                self.lifecycle.record(
                    "health_check_failed",
                    server_name=self.name,
                    session_id=self._session_id,
                    status=MCPHealthStatus.HEALTH_CHECK_TIMEOUT,
                    stage="initialize",
                    error=exc,
                )
                raise
            except Exception as exc:
                self.lifecycle.record(
                    "health_check_failed",
                    server_name=self.name,
                    session_id=self._session_id,
                    status=MCPHealthStatus.INITIALIZATION_FAILED,
                    stage="initialize",
                    error=exc,
                )
                raise
            try:
                await asyncio.wait_for(session.list_tools(), timeout=self.health_timeout_seconds)
            except TimeoutError as exc:
                self.lifecycle.record(
                    "health_check_failed",
                    server_name=self.name,
                    session_id=self._session_id,
                    status=MCPHealthStatus.HEALTH_CHECK_TIMEOUT,
                    stage="capability_list",
                    error=exc,
                )
                raise
            except Exception as exc:
                status = (
                    MCPHealthStatus.CLIENT_CLOSED if is_mcp_transport_error(exc) else MCPHealthStatus.SERVER_UNAVAILABLE
                )
                self.lifecycle.record(
                    "health_check_failed",
                    server_name=self.name,
                    session_id=self._session_id,
                    status=status,
                    stage="capability_list",
                    error=exc,
                )
                raise
            self._healthy = True
            self.lifecycle.record(
                "health_check_succeeded",
                server_name=self.name,
                session_id=self._session_id,
                status=MCPHealthStatus.HEALTHY,
                stage="capability_list",
                generation=generation,
            )
            interceptor = self._recovery_interceptor(generation)
            return await asyncio.wait_for(
                self._tool_loader(
                    session,
                    server_name=self.name,
                    tool_name_prefix=self.tool_name_prefix,
                    tool_interceptors=[interceptor],
                ),
                timeout=self.health_timeout_seconds,
            )
        except Exception:
            await self.close_generation()
            raise

    def _recovery_interceptor(self, bound_generation: int):
        async def recover(request, handler):
            if self._session is None or not self._healthy or bound_generation != self._generation:
                return await self._call_current(request)
            try:
                return await handler(request)
            except Exception as exc:
                if not is_mcp_transport_error(exc):
                    raise
                return await self._recover_midcase(request, exc)

        return recover

    async def _call_current(self, request):
        if self._session is None or not self._healthy:
            raise MCPInfrastructureError("MCP client is unavailable")
        return await self._session.call_tool(request.name, request.args)

    async def _recover_midcase(self, request, exc: BaseException):
        self.lifecycle.infrastructure_degraded = True
        self.lifecycle.metrics.mcp_midcase_disconnects += 1
        self.lifecycle.failure_stage = self.lifecycle.failure_stage or "mid_case_tool_call"
        old_session_id = self._session_id
        self.lifecycle.record(
            "midcase_disconnect",
            server_name=self.name,
            session_id=old_session_id,
            stage="tool_call",
            error=exc,
            tool_name=request.name,
        )
        if not self._recreate_allowed():
            self.lifecycle.recovery_exhausted = True
            raise MCPInfrastructureError("MCP reconnect budget exhausted") from exc
        self.lifecycle.metrics.mcp_reconnect_attempts += 1
        await self.close_generation()
        try:
            await self._open_generation(reconnect=True)
        except Exception as retry_exc:
            self.lifecycle.metrics.mcp_reconnect_failures += 1
            self.lifecycle.recovery_exhausted = True
            self.lifecycle.record(
                "reconnect_failed",
                server_name=self.name,
                session_id=self._session_id,
                stage="mid_case_recreate",
                error=retry_exc,
                previous_session_id=old_session_id,
            )
            raise MCPInfrastructureError("MCP mid-case recreate failed") from retry_exc
        self.lifecycle.metrics.mcp_reconnect_successes += 1
        self.lifecycle.record(
            "reconnect_succeeded",
            server_name=self.name,
            session_id=self._session_id,
            stage="mid_case_recreate",
            previous_session_id=old_session_id,
        )
        if request.name in _NON_RETRYABLE_ACTIVE_TOOLS:
            # The new session is healthy for subsequent calls, but the packet
            # action is not replayed because delivery before disconnect is unknown.
            raise MCPInfrastructureError("Active probe was not replayed after MCP disconnect") from exc
        self.lifecycle.record(
            "tool_retry",
            server_name=self.name,
            session_id=self._session_id,
            stage="mid_case_retry",
            tool_name=request.name,
            previous_session_id=old_session_id,
        )
        try:
            return await self._call_current(request)
        except Exception as retry_exc:
            if not is_mcp_transport_error(retry_exc):
                raise
            self.lifecycle.metrics.mcp_reconnect_failures += 1
            self.lifecycle.recovery_exhausted = True
            self.lifecycle.failure_stage = "mid_case_retry"
            self.lifecycle.record(
                "tool_retry_failed",
                server_name=self.name,
                session_id=self._session_id,
                stage="mid_case_retry",
                error=retry_exc,
                tool_name=request.name,
                previous_session_id=old_session_id,
            )
            raise MCPInfrastructureError("MCP retry failed after bounded reconnect") from retry_exc

    async def close_generation(self) -> None:
        stack, session_id = self._stack, self._session_id
        had_session = self._session is not None
        self._stack = None
        self._session = None
        self._healthy = False
        if stack is None:
            return
        try:
            await asyncio.wait_for(stack.aclose(), timeout=self.health_timeout_seconds)
        except Exception as exc:
            self.lifecycle.infrastructure_degraded = True
            self.lifecycle.record(
                "client_close_failed",
                server_name=self.name,
                session_id=session_id,
                stage="case_cleanup",
                error=exc,
            )
        finally:
            if had_session:
                self.lifecycle.metrics.mcp_session_closed += 1
                self.lifecycle.record("session_closed", server_name=self.name, session_id=session_id)
            self.lifecycle.metrics.mcp_client_closed += 1
            self.lifecycle.record("client_closed", server_name=self.name, session_id=session_id)


class MCPCaseClient:
    """Own all MCP client generations for one benchmark case."""

    def __init__(
        self,
        server_config: dict[str, dict[str, Any]],
        lifecycle: MCPLifecycleTrace,
        *,
        health_timeout_seconds: float = 10.0,
        max_recreates: int = 1,
        session_factory=create_session,
        tool_loader=load_mcp_tools,
    ) -> None:
        self.server_config = server_config
        self.lifecycle = lifecycle
        self.health_timeout_seconds = health_timeout_seconds
        self.max_recreates = max(0, max_recreates)
        self._recreates_used = 0
        self._connections = [
            _ManagedMCPConnection(
                name,
                connection,
                lifecycle,
                health_timeout_seconds=health_timeout_seconds,
                tool_name_prefix=len(server_config) > 1,
                recreate_allowed=self._claim_recreate,
                session_factory=session_factory,
                tool_loader=tool_loader,
            )
            for name, connection in server_config.items()
        ]
        self.tools: list[Any] = []

    def _claim_recreate(self) -> bool:
        if self._recreates_used >= self.max_recreates:
            return False
        self._recreates_used += 1
        return True

    async def __aenter__(self) -> MCPCaseClient:
        try:
            for connection in self._connections:
                tools = await connection.open()
                self.tools.extend(tools)
            return self
        except Exception:
            for connection in reversed(self._connections):
                await connection.close_generation()
            self.lifecycle.finalize()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        for connection in reversed(self._connections):
            await connection.close_generation()
        self.lifecycle.finalize()
        return False


class SchemaErrorDedupMiddleware(AgentMiddleware):
    """Execute an invalid tool signature once, then fail closed on repeats."""

    def __init__(self) -> None:
        self._failed_signatures: set[str] = set()

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        signature = json.dumps(
            {"name": call.get("name"), "args": call.get("args") or {}},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if signature in self._failed_signatures:
            return ToolMessage(
                content=(
                    "Duplicate invalid tool call blocked. The same action signature already failed schema "
                    "validation; choose supported arguments or a different tool."
                ),
                tool_call_id=str(call.get("id") or "duplicate-invalid-tool-call"),
                name=str(call.get("name") or "tool"),
                status="error",
            )
        result = await handler(request)
        if _is_schema_error(result):
            self._failed_signatures.add(signature)
        return result


def _is_schema_error(message: Any) -> bool:
    if not isinstance(message, ToolMessage) or message.status != "error":
        return False
    text = str(message.content).lower()
    return any(
        marker in text
        for marker in (
            "unexpected keyword argument",
            "unexpected_keyword_argument",
            "validation error",
            "extra_forbidden",
            "invalid arguments for tool",
        )
    )


async def _connect_mcp_tools(
    exit_stack: AsyncExitStack,
    server_config: dict[str, dict[str, Any]],
    *,
    lifecycle: MCPLifecycleTrace | None = None,
    health_timeout_seconds: float = 10.0,
    max_recreates: int = 1,
) -> list:
    """Open case-scoped, health-checked MCP sessions and return tools."""
    trace = lifecycle or MCPLifecycleTrace(case_id="unrecorded-case")
    manager = MCPCaseClient(
        server_config,
        trace,
        health_timeout_seconds=health_timeout_seconds,
        max_recreates=max_recreates,
    )
    managed = await exit_stack.enter_async_context(manager)
    return managed.tools


__all__ = [
    "MCPCaseClient",
    "MCPHealthStatus",
    "MCPInfrastructureError",
    "MCPLifecycleMetrics",
    "MCPLifecycleTrace",
    "SchemaErrorDedupMiddleware",
    "_connect_mcp_tools",
    "is_mcp_transport_error",
]
