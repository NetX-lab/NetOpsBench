"""Trace-aware runtime helpers exposed to diagnostic agents."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from netopsbench.agents._trace_utils import jsonable as _jsonable


class AgentTraceRecorder:
    """Per-diagnosis recorder exposed to agents as ``context.trace``."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._steps: list[dict[str, Any]] = []
        self._pending_llm: dict[str, dict[str, Any]] = {}
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "llm_call_count": 0,
        }
        self._model: dict[str, Any] = {}
        self._lock = threading.Lock()

    @classmethod
    def disabled(cls) -> AgentTraceRecorder:
        """Return a recorder that preserves the API without collecting trace data."""

        return cls(enabled=False)

    def llm_client(
        self,
        provider: str = "openai",
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **client_kwargs: Any,
    ) -> TraceAwareLLMClient:
        """Return an OpenAI-compatible chat client that records visible messages."""

        return TraceAwareLLMClient(
            recorder=self,
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            client_kwargs=client_kwargs,
        )

    def langchain_callback(self) -> Any | None:
        """Return a LangChain callback handler that writes into this recorder."""

        if not self.enabled:
            return None
        try:
            from langchain_core.callbacks.base import BaseCallbackHandler
        except Exception as exc:  # pragma: no cover - depends on optional agent deps
            raise RuntimeError("langchain-core is required for context.trace.langchain_callback()") from exc

        recorder = self

        class LangChainTraceCallback(BaseCallbackHandler):
            def on_chat_model_start(
                self,
                serialized: dict[str, Any],
                messages: list[list[Any]],
                *,
                run_id: Any,
                parent_run_id: Any = None,
                invocation_params: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> None:
                del serialized, kwargs
                flat_messages = messages[0] if messages else []
                recorder.record_llm_request(
                    flat_messages,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    model=(invocation_params or {}).get("model") or (invocation_params or {}).get("model_name"),
                    provider=(invocation_params or {}).get("provider") or (invocation_params or {}).get("vendor"),
                )

            def on_llm_start(
                self,
                serialized: dict[str, Any],
                prompts: list[str],
                *,
                run_id: Any,
                parent_run_id: Any = None,
                invocation_params: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> None:
                del serialized, kwargs
                recorder.record_llm_request(
                    [{"role": "user", "content": prompt} for prompt in prompts],
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    model=(invocation_params or {}).get("model") or (invocation_params or {}).get("model_name"),
                    provider=(invocation_params or {}).get("provider") or (invocation_params or {}).get("vendor"),
                )

            def on_llm_end(self, response: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any) -> None:
                del kwargs
                recorder.record_llm_response(response, run_id=run_id, parent_run_id=parent_run_id)

            def on_llm_error(
                self, error: BaseException, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any
            ) -> None:
                del kwargs
                recorder.record_error(
                    stage="llm",
                    error=error,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                )

            def on_tool_start(
                self,
                serialized: dict[str, Any],
                input_str: str,
                *,
                run_id: Any,
                parent_run_id: Any = None,
                inputs: dict[str, Any] | None = None,
                **kwargs: Any,
            ) -> None:
                recorder.record_tool_start(
                    name=kwargs.get("name") or (serialized or {}).get("name") or "tool",
                    args=inputs if inputs is not None else input_str,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                )

            def on_tool_end(self, output: Any, *, run_id: Any, parent_run_id: Any = None, **kwargs: Any) -> None:
                del kwargs
                recorder.record_tool_end(output=output, run_id=run_id, parent_run_id=parent_run_id)

            def on_tool_error(
                self,
                error: BaseException,
                *,
                run_id: Any,
                parent_run_id: Any = None,
                **kwargs: Any,
            ) -> None:
                del kwargs
                recorder.record_tool_error(error=error, run_id=run_id, parent_run_id=parent_run_id)

        return LangChainTraceCallback()

    def record_llm_request(
        self,
        messages: list[Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> str:
        call_id = str(run_id or uuid.uuid4())
        if not self.enabled:
            return call_id
        if model or provider:
            self._remember_model(model=model, provider=provider)
        span = {
            "run_id": call_id,
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "started_at": _isoformat(datetime.now(UTC)),
            "model": model,
            "provider": provider,
            "extra": {
                "llm_request": {
                    "messages": [
                        _message_payload(message, index=index) for index, message in enumerate(messages or [], 1)
                    ]
                }
            },
        }
        with self._lock:
            self._pending_llm[call_id] = _jsonable(span)
        return call_id

    def record_llm_response(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        call_id = str(run_id or uuid.uuid4())
        if model or provider:
            self._remember_model(model=model, provider=provider)
        ended_at = _isoformat(datetime.now(UTC))
        messages = _response_messages(response)
        usage = _response_token_usage(response, messages)
        self._accumulate_usage(usage)
        with self._lock:
            span = self._pending_llm.pop(call_id, {})
            for message in messages or [{"role": "assistant", "content": _response_text(response)}]:
                response_payload = _response_message_payload(message)
                extra = dict(span.get("extra") or {})
                extra["llm_response"] = response_payload
                step = {
                    "type": "llm",
                    "source": "agent",
                    "message": _llm_step_message(message, response=response),
                    "run_id": call_id,
                    "parent_run_id": str(parent_run_id) if parent_run_id else span.get("parent_run_id"),
                    "started_at": span.get("started_at"),
                    "ended_at": ended_at,
                    "duration_seconds": _duration_seconds(span.get("started_at"), ended_at),
                    "usage": {
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                    },
                    "extra": extra,
                }
                reasoning_content = response_payload.get("reasoning_content")
                if reasoning_content:
                    step["reasoning_content"] = reasoning_content
                step_model = model or span.get("model") or self._model.get("model")
                step_provider = provider or span.get("provider") or self._model.get("provider")
                if step_model:
                    step["model"] = step_model
                if step_provider:
                    step["provider"] = step_provider
                self._steps.append(_jsonable(step))

    def record_tool_start(
        self,
        *,
        name: str,
        args: Any = None,
        run_id: Any = None,
        parent_run_id: Any = None,
    ) -> str:
        call_id = str(run_id or uuid.uuid4())
        if not self.enabled:
            return call_id
        step = {
            "type": "tool_call",
            "name": name or "tool",
            "args": _jsonable(args if args is not None else {}),
            "tool_call_id": call_id,
            "run_id": call_id,
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "started_at": _isoformat(datetime.now(UTC)),
        }
        with self._lock:
            self._pending_tools[call_id] = step
            self._steps.append(step)
        return call_id

    def record_tool_end(self, *, output: Any, run_id: Any, parent_run_id: Any = None) -> None:
        del parent_run_id
        if not self.enabled:
            return
        call_id = str(run_id)
        with self._lock:
            step = self._pending_tools.pop(call_id, None)
            if step is None:
                step = {
                    "type": "tool_call",
                    "name": "tool",
                    "tool_call_id": call_id,
                    "run_id": call_id,
                }
                self._steps.append(step)
            step["observation"] = _jsonable(output)
            step["ended_at"] = _isoformat(datetime.now(UTC))
            step["duration_seconds"] = _duration_seconds(step.get("started_at"), step.get("ended_at"))

    def record_tool_error(self, *, error: BaseException, run_id: Any, parent_run_id: Any = None) -> None:
        del parent_run_id
        if not self.enabled:
            return
        call_id = str(run_id)
        with self._lock:
            step = self._pending_tools.pop(call_id, None)
            if step is None:
                step = {
                    "type": "tool_call",
                    "name": "tool",
                    "tool_call_id": call_id,
                    "run_id": call_id,
                }
                self._steps.append(step)
            step["error"] = str(error)
            step["ended_at"] = _isoformat(datetime.now(UTC))
            step["duration_seconds"] = _duration_seconds(step.get("started_at"), step.get("ended_at"))

    def record_error(
        self, *, stage: str, error: BaseException | str, run_id: Any = None, parent_run_id: Any = None
    ) -> None:
        if not self.enabled:
            return
        call_id = str(run_id) if run_id else None
        ended_at = _isoformat(datetime.now(UTC))
        with self._lock:
            span = self._pending_llm.pop(call_id, {}) if call_id and stage == "llm" else {}
            self._steps.append(
                _jsonable(
                    {
                        "type": "error",
                        "source": "agent",
                        "message": str(error),
                        "run_id": call_id,
                        "parent_run_id": str(parent_run_id) if parent_run_id else span.get("parent_run_id"),
                        "started_at": span.get("started_at"),
                        "ended_at": ended_at,
                        "duration_seconds": _duration_seconds(span.get("started_at"), ended_at),
                        "extra": {
                            **dict(span.get("extra") or {}),
                            "stage": stage,
                            "error_type": type(error).__name__ if isinstance(error, BaseException) else "Error",
                        },
                    }
                )
            )

    def to_steps(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_jsonable(step) for step in self._steps]

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def model_metadata(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._model)

    def tool_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        with self._lock:
            for step in self._steps:
                if not _is_tool_step(step):
                    continue
                calls.append(
                    {
                        "tool": step.get("name") or step.get("tool") or "tool",
                        "args": step.get("args") or step.get("input") or {},
                        "tool_call_id": step.get("tool_call_id") or step.get("run_id"),
                    }
                )
        return calls

    def _remember_model(self, *, model: str | None = None, provider: str | None = None) -> None:
        with self._lock:
            if model:
                self._model["model"] = model
            if provider:
                self._model["provider"] = provider

    def _accumulate_usage(self, usage: dict[str, int]) -> None:
        with self._lock:
            self._metrics["input_tokens"] += usage["input_tokens"]
            self._metrics["output_tokens"] += usage["output_tokens"]
            self._metrics["total_tokens"] += usage["total_tokens"]
            self._metrics["llm_call_count"] += usage["has_usage"] or 1


class TraceAwareLLMClient:
    """Small OpenAI-compatible chat client wrapper that records requests and responses."""

    def __init__(
        self,
        *,
        recorder: AgentTraceRecorder,
        provider: str,
        model: str,
        api_key: str | None,
        base_url: str | None,
        client_kwargs: dict[str, Any],
    ):
        self.recorder = recorder
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.client_kwargs = dict(client_kwargs)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        try:
            from openai import AsyncOpenAI
        except Exception as exc:  # pragma: no cover - depends on optional agent deps
            raise RuntimeError("openai is required for context.trace.llm_client().chat()") from exc

        run_id = self.recorder.record_llm_request(
            messages,
            model=self.model,
            provider=self.provider,
        )
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, **self.client_kwargs)
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=cast(Any, messages),
                **kwargs,
            )
        except Exception as exc:
            self.recorder.record_error(stage="llm", error=exc, run_id=run_id)
            raise
        self.recorder.record_llm_response(
            response,
            run_id=run_id,
            model=self.model,
            provider=self.provider,
        )
        return response


def _message_payload(message: Any, *, index: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "index": index,
        "role": _message_role(message),
        "source": _message_source(_message_role(message)),
        "content": _jsonable(_message_attr(message, "content") or ""),
    }
    for key in ("name", "tool_call_id", "tool_calls"):
        value = _message_attr(message, key)
        if value is not None:
            payload[key] = _jsonable(value)
    return payload


def _response_message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": _message_role(message),
        "source": _message_source(_message_role(message)),
        "content": _jsonable(_message_attr(message, "content") or ""),
    }
    tool_calls = _message_tool_calls(message)
    if tool_calls:
        payload["tool_calls"] = _jsonable(tool_calls)
    for key in ("additional_kwargs", "response_metadata", "usage_metadata"):
        value = _message_attr(message, key)
        if value:
            payload[key] = _jsonable(value)
    reasoning_content = _message_reasoning_content(message)
    if reasoning_content:
        payload["reasoning_content"] = reasoning_content
    return payload


def _message_attr(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _message_role(message: Any) -> str:
    return str(_message_attr(message, "type") or _message_attr(message, "role") or "message").lower()


def _message_source(role: str) -> str:
    if role == "human":
        return "user"
    if role in {"ai", "assistant"}:
        return "agent"
    if role in {"system", "user", "agent"}:
        return role
    return "agent"


def _message_text(message: Any) -> str:
    content = _message_attr(message, "content")
    if content is None:
        return ""
    return content if isinstance(content, str) else json.dumps(_jsonable(content), sort_keys=True, default=str)


def _llm_step_message(message: Any, *, response: Any) -> str:
    text = _message_text(message) or _response_text(response)
    if text:
        return text
    return ""


def _message_tool_calls(message: Any) -> Any:
    tool_calls = _message_attr(message, "tool_calls")
    if tool_calls:
        return tool_calls
    additional_kwargs = _message_attr(message, "additional_kwargs") or {}
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("tool_calls"):
        return additional_kwargs["tool_calls"]
    return []


def _message_reasoning_content(message: Any) -> str:
    reasoning_content = _message_attr(message, "reasoning_content")
    if reasoning_content:
        return str(reasoning_content)
    model_extra = _message_attr(message, "model_extra") or {}
    if isinstance(model_extra, dict) and model_extra.get("reasoning_content"):
        return str(model_extra["reasoning_content"])
    additional_kwargs = _message_attr(message, "additional_kwargs") or {}
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("reasoning_content"):
        return str(additional_kwargs["reasoning_content"])
    return ""


def _message_token_usage(message: Any) -> dict[str, int]:
    usage = _message_attr(message, "usage_metadata") or {}
    response_metadata = _message_attr(message, "response_metadata") or {}
    token_usage = (response_metadata.get("token_usage") or {}) if isinstance(response_metadata, dict) else {}
    input_tokens = _safe_int(usage.get("input_tokens", token_usage.get("prompt_tokens", 0)))
    output_tokens = _safe_int(usage.get("output_tokens", token_usage.get("completion_tokens", 0)))
    total = usage.get("total_tokens", token_usage.get("total_tokens"))
    total_tokens = _safe_int(total) if total is not None else input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "has_usage": int(any((input_tokens, output_tokens, total_tokens))),
    }


def _response_messages(response: Any) -> list[Any]:
    choices = _message_attr(response, "choices") or []
    messages = [_message_attr(choice, "message") for choice in choices if _message_attr(choice, "message") is not None]
    if messages:
        return messages

    generations = _message_attr(response, "generations") or []
    extracted: list[Any] = []
    for group in generations or []:
        for generation in group or []:
            message = _message_attr(generation, "message")
            if message is not None:
                extracted.append(message)
                continue
            text = _message_attr(generation, "text")
            if text is not None:
                extracted.append({"role": "assistant", "content": text})
    return extracted


def _response_token_usage(response: Any, messages: list[Any]) -> dict[str, int]:
    usage = _message_attr(response, "usage")
    if usage is not None:
        input_tokens = _safe_int(_message_attr(usage, "prompt_tokens") or _message_attr(usage, "input_tokens"))
        output_tokens = _safe_int(_message_attr(usage, "completion_tokens") or _message_attr(usage, "output_tokens"))
        total_tokens = _safe_int(_message_attr(usage, "total_tokens")) or input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "has_usage": int(any((input_tokens, output_tokens, total_tokens))),
        }
    counts = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "has_usage": 0}
    for message in messages:
        message_usage = _message_token_usage(message)
        counts["input_tokens"] += message_usage["input_tokens"]
        counts["output_tokens"] += message_usage["output_tokens"]
        counts["total_tokens"] += message_usage["total_tokens"]
        counts["has_usage"] = max(counts["has_usage"], message_usage["has_usage"])
    return counts


def _response_text(response: Any) -> str:
    text = _message_attr(response, "text")
    if text is not None:
        return str(text)
    content = _message_attr(response, "content")
    if content is not None:
        return content if isinstance(content, str) else json.dumps(_jsonable(content), sort_keys=True, default=str)
    return ""


def _is_tool_step(step: dict[str, Any]) -> bool:
    if str(step.get("type") or "").lower() in {"tool", "tool_call"}:
        return True
    if step.get("tool_call_id"):
        return True
    return bool(step.get("name") and ("args" in step or "input" in step))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _duration_seconds(started_at: Any, ended_at: Any) -> float | None:
    try:
        if not started_at or not ended_at:
            return None
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return None


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


__all__ = ["AgentTraceRecorder"]
