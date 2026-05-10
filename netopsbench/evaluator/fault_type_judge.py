"""Fault-type semantic judging helpers.

This module keeps LLM-as-judge support narrow and auditable: the judge only
decides whether an agent's fault-type description maps to the same canonical
NetOpsBench fault type as ground truth.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from netopsbench.platform.faults.specs import canonicalize_fault_name, get_supported_scenario_faults

if TYPE_CHECKING:
    from netopsbench.config import FaultTypeJudgeConfig


UNKNOWN_FAULT_TYPE = "unknown"

_LEGACY_FAULT_TYPE_ALIASES: dict[str, str] = {
    "link_failure": "link_down",
    "linkdown": "link_down",
    "link failure": "link_down",
    "blackhole": "blackhole_route",
    "black_hole": "blackhole_route",
    "static_route_error": "static_route_misconfig",
    "static_route_misconfiguration": "static_route_misconfig",
    "route_misconfig": "static_route_misconfig",
    "mtu_error": "mtu_mismatch",
    "bgp_peer_misconfig": "bgp_neighbor_misconfig",
    "bgp_peer_config_error": "bgp_neighbor_misconfig",
    "bgp_session_misconfig": "bgp_neighbor_misconfig",
    "bgp_neighbor_config_error": "bgp_neighbor_misconfig",
    "route_policy_error": "route_policy_misconfig",
    "route_policy_misconfiguration": "route_policy_misconfig",
    "bgp_policy_misconfig": "route_policy_misconfig",
    "route_origination_missing": "route_policy_misconfig",
    "network_statement_missing": "route_policy_misconfig",
    "prefix_filter_misconfig": "route_policy_misconfig",
    "outbound_prefix_filter": "route_policy_misconfig",
    "acl_misconfiguration": "acl_misconfig",
}


def supported_fault_types() -> list[str]:
    """Return canonical fault types allowed in scenario evaluation."""
    return sorted(set(get_supported_scenario_faults()))


def canonicalize_fault_type(fault_type: str | None) -> str:
    """Canonicalize a free-form fault type using registry aliases plus legacy evaluator aliases."""
    raw = str(fault_type or "").strip()
    if not raw:
        return ""
    normalized = raw.lower().strip().replace(" ", "_")
    normalized = _LEGACY_FAULT_TYPE_ALIASES.get(normalized, normalized)
    return canonicalize_fault_name(normalized) or normalized


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_evidence(evidence: Sequence[str], *, max_items: int = 6, max_chars: int = 240) -> list[str]:
    return [_truncate_text(item, max_chars) for item in list(evidence or [])[:max_items]]


class FaultTypeJudgeRequest(BaseModel):
    """Structured input sent to a fault-type judge."""

    agent_fault_type: str = ""
    ground_truth_fault_type: str
    canonical_agent_fault_type: str = ""
    canonical_ground_truth_fault_type: str
    allowed_fault_types: list[str]
    agent_reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)


class FaultTypeJudgeResult(BaseModel):
    """Structured output returned by a fault-type judge."""

    canonical_agent_fault_type: str
    canonical_ground_truth_fault_type: str
    is_match: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    matched_alias_or_description: str | None = None
    taxonomy_violation: bool = False
    judge_model: str | None = None

    @field_validator("canonical_agent_fault_type", "canonical_ground_truth_fault_type")
    @classmethod
    def _normalize_canonical_field(cls, value: str) -> str:
        return canonicalize_fault_type(value) or UNKNOWN_FAULT_TYPE

    @model_validator(mode="after")
    def _validate_taxonomy(self) -> FaultTypeJudgeResult:
        allowed = set(supported_fault_types()) | {UNKNOWN_FAULT_TYPE, ""}
        if self.canonical_agent_fault_type not in allowed or self.canonical_ground_truth_fault_type not in allowed:
            self.taxonomy_violation = True
            self.is_match = False
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class FaultTypeJudge(Protocol):
    """Protocol implemented by fault-type judge strategies."""

    def judge(self, request: FaultTypeJudgeRequest) -> FaultTypeJudgeResult:
        """Return a taxonomy-constrained judgment for the request."""


JudgeCallable = Callable[[FaultTypeJudgeRequest], FaultTypeJudgeResult | Mapping[str, Any] | str]


class StructuredFaultTypeJudge:
    """Pydantic-backed judge wrapper around an injected structured-output callable.

    The callable may be a test double, a LangChain structured-output wrapper, or
    a provider-specific client. It must return a ``FaultTypeJudgeResult``, a
    mapping compatible with that model, or a JSON string compatible with it.
    """

    def __init__(self, judge_fn: JudgeCallable, *, model: str | None = None):
        self._judge_fn = judge_fn
        self.model = model

    def judge(self, request: FaultTypeJudgeRequest) -> FaultTypeJudgeResult:
        raw_result = self._judge_fn(request)
        if isinstance(raw_result, FaultTypeJudgeResult):
            result = raw_result
        elif isinstance(raw_result, str):
            result = FaultTypeJudgeResult.model_validate(json.loads(raw_result))
        else:
            result = FaultTypeJudgeResult.model_validate(dict(raw_result))
        if result.judge_model is None:
            result.judge_model = self.model
        return result


def build_fault_type_judge_request(
    *,
    agent_fault_type: str | None,
    ground_truth_fault_type: str,
    agent_reasoning: str = "",
    evidence: Sequence[str] | None = None,
) -> FaultTypeJudgeRequest:
    """Build the compact, taxonomy-aware request passed to a judge."""
    allowed_fault_types = supported_fault_types()
    return FaultTypeJudgeRequest(
        agent_fault_type=str(agent_fault_type or ""),
        ground_truth_fault_type=str(ground_truth_fault_type or ""),
        canonical_agent_fault_type=canonicalize_fault_type(agent_fault_type),
        canonical_ground_truth_fault_type=canonicalize_fault_type(ground_truth_fault_type),
        allowed_fault_types=allowed_fault_types,
        agent_reasoning=_truncate_text(agent_reasoning, 1200),
        evidence=_compact_evidence(evidence or []),
    )


def judge_fault_type_match(
    *,
    judge: FaultTypeJudge | None,
    agent_fault_type: str | None,
    ground_truth_fault_type: str,
    agent_reasoning: str = "",
    evidence: Sequence[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Judge fault-type correctness with deterministic fallback and optional LLM judge."""
    request = build_fault_type_judge_request(
        agent_fault_type=agent_fault_type,
        ground_truth_fault_type=ground_truth_fault_type,
        agent_reasoning=agent_reasoning,
        evidence=evidence,
    )
    details: dict[str, Any] = {
        "mode": "deterministic",
        "agent_fault_type": request.agent_fault_type,
        "ground_truth_fault_type": request.ground_truth_fault_type,
        "canonical_agent_fault_type": request.canonical_agent_fault_type,
        "canonical_ground_truth_fault_type": request.canonical_ground_truth_fault_type,
    }

    if request.canonical_agent_fault_type and (
        request.canonical_agent_fault_type == request.canonical_ground_truth_fault_type
    ):
        details.update({"is_match": True, "confidence": 1.0, "reasoning": "canonical fault types match"})
        return True, details

    if judge is None:
        details.update({"is_match": False, "confidence": 1.0, "reasoning": "canonical fault types differ"})
        return False, details

    try:
        judgment = judge.judge(request)
    except (ValidationError, json.JSONDecodeError, ValueError, TypeError) as exc:
        details.update(
            {
                "mode": "judge_error",
                "is_match": False,
                "confidence": 0.0,
                "reasoning": f"fault type judge failed: {type(exc).__name__}",
            }
        )
        return False, details

    judgment_dict = judgment.to_dict()
    judgment_dict["mode"] = "llm_judge"
    if judgment.taxonomy_violation:
        judgment_dict["is_match"] = False
    if judgment.canonical_ground_truth_fault_type != request.canonical_ground_truth_fault_type:
        judgment_dict["ground_truth_canonical_mismatch"] = True
        judgment_dict["is_match"] = False
    if judgment_dict["is_match"] and judgment.canonical_agent_fault_type != request.canonical_ground_truth_fault_type:
        judgment_dict["agent_canonical_mismatch"] = True
        judgment_dict["is_match"] = False
    return bool(judgment_dict["is_match"]), judgment_dict


def create_judge_from_env(
    cfg: FaultTypeJudgeConfig | None = None,
) -> StructuredFaultTypeJudge | None:
    """Return a :class:`StructuredFaultTypeJudge` backed by a LangChain chat model.

    Configuration is read from a :class:`~netopsbench.config.FaultTypeJudgeConfig`
    instance (defaults to the global :data:`~netopsbench.config.config` singleton).
    If ``cfg.enabled`` is ``False`` (the default), returns ``None`` and the
    evaluator falls back to deterministic string matching.

    The judge uses ``langchain_openai.ChatOpenAI`` with
    ``.with_structured_output(FaultTypeJudgeResult)`` so that schema validation is
    enforced at the model layer rather than by post-hoc JSON parsing.

    Supported providers via ``NETOPSBENCH_FAULT_TYPE_JUDGE_BASE_URL``:
    - OpenAI (default, leave ``base_url`` unset)
    - DeepSeek: ``https://api.deepseek.com``
    - Kimi / Moonshot: ``https://api.moonshot.cn/v1``
    - Any other OpenAI-compatible endpoint (``base_url`` + ``api_key``)

    The ``api_key`` field falls back to the ``OPENAI_API_KEY`` environment variable
    when ``NETOPSBENCH_FAULT_TYPE_JUDGE_API_KEY`` is not set.
    """
    if cfg is None:
        from netopsbench.config import config as _global_config

        cfg = _global_config.fault_type_judge_config

    if not cfg.enabled:
        return None

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'langchain-openai' package is required for LLM-as-judge fault type evaluation. "
            "Install it with: pip install langchain-openai"
        ) from exc

    from langchain_core.messages import HumanMessage, SystemMessage

    model = cfg.model
    llm_kwargs: dict[str, Any] = {"model": model, "temperature": 0}
    if cfg.api_key:
        llm_kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        llm_kwargs["base_url"] = cfg.base_url

    structured_llm = ChatOpenAI(**llm_kwargs).with_structured_output(FaultTypeJudgeResult, method="function_calling")

    def _call_langchain(request: FaultTypeJudgeRequest) -> FaultTypeJudgeResult:
        prompt = build_fault_type_judge_prompt(request)
        result = structured_llm.invoke(
            [
                SystemMessage(
                    content="You are a precise fault-type taxonomy judge for network operations benchmarking."
                ),
                HumanMessage(content=prompt),
            ]
        )
        return cast(FaultTypeJudgeResult, result)

    return StructuredFaultTypeJudge(_call_langchain, model=model)


def build_fault_type_judge_prompt(request: FaultTypeJudgeRequest) -> str:
    """Build a provider-agnostic prompt for structured fault-type judging."""
    allowed = ", ".join(request.allowed_fault_types)
    evidence = "\n".join(f"- {item}" for item in request.evidence) or "- none"
    return f"""You are judging only the fault type label for a NetOpsBench diagnosis.

Decide whether the agent's fault type is semantically equivalent to the ground-truth canonical fault type.
Do not judge verdict, device, interface, confidence, or evidence quality. Only map the fault type phrase to the allowed taxonomy.

Allowed canonical fault types: {allowed}

Ground truth fault type: {request.ground_truth_fault_type}
Canonical ground truth fault type: {request.canonical_ground_truth_fault_type}

Agent fault type: {request.agent_fault_type or "<empty>"}
Agent canonical fast-path guess: {request.canonical_agent_fault_type or UNKNOWN_FAULT_TYPE}

Agent reasoning:
{request.agent_reasoning or "<empty>"}

Agent evidence:
{evidence}

If the agent phrase maps to a canonical type outside the allowed taxonomy, set canonical_agent_fault_type="unknown", is_match=false, and taxonomy_violation=true.
"""
