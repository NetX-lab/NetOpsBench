"""Pydantic output schema shared by all providers."""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from netopsbench.platform.faults.specs import get_supported_scenario_faults
from netopsbench.sdk import build_canonical_observation
from netopsbench.sdk.agents import DiagnosisResult


class DiagnosisLocation(BaseModel):
    device: str | None = None
    interface: str | None = None


class DiagnosisOutput(BaseModel):
    # The benchmark scorer requires an exact string match against one of these
    # three values.  Using Literal advertises the enum to the LLM through the
    # tool's JSON schema and lets pydantic reject free-form synonyms like
    # "fault", "fault_found", "fault_confirmed" that earlier runs produced.
    verdict: Literal["fault_detected", "network_healthy", "inconclusive"] = Field(
        description=(
            "Final diagnosis verdict. Must be exactly one of: "
            "'fault_detected' (a fault is present), "
            "'network_healthy' (no fault), "
            "'inconclusive' (insufficient evidence). "
            "Do NOT invent other values such as 'fault', 'fault_found', or 'fault_confirmed'."
        )
    )
    fault_type: str | None = Field(
        default=None, description="Canonical scored category: " + ", ".join(get_supported_scenario_faults())
    )
    location: DiagnosisLocation = Field(default_factory=DiagnosisLocation)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""


SYSTEM_GUIDANCE = (
    "You are a production network troubleshooting expert for DCN fabrics. "
    "Use NetOpsBench MCP tools to diagnose live issues with evidence-first reasoning. "
    "Do not assume hidden ground truth. "
    "The DiagnosisOutput.verdict field is a strict enum — it MUST be exactly one of "
    "'fault_detected', 'network_healthy', or 'inconclusive'. "
    "Do NOT use synonyms such as 'fault', 'fault_found', 'fault_confirmed', or 'fault_resolved' — "
    "any of those will be scored as wrong. "
    "If evidence is insufficient, return verdict='inconclusive' with an empty location. "
    "Use the initial Pingmesh symptoms and normal topology to guide investigation. "
    "Query diagnostic tools for device state, routing, BGP, interfaces and ACL evidence. "
    "Healthy latency/loss can coexist with missing routes or fewer FIB next hops. "
    "Validate the route RIB, advertisements and kernel FIB, and distinguish the supported fault categories. "
    "Be efficient: avoid redundant tool calls and do not repeat the same query. "
    "You have a limited tool-call budget — focus on the most informative tools first. "
)


def build_user_prompt(context: Any, *, fenced_output: bool = True) -> str:
    metadata = getattr(context, "metadata", {}) or {}
    observation = metadata.get("canonical_observation")
    if not isinstance(observation, dict):
        observation = build_canonical_observation(
            case_id=str(getattr(context, "scenario_id", "unknown")),
            topology=dict(getattr(context, "topology", {}) or {}),
            symptoms=dict(getattr(context, "symptoms", {}) or {}),
        )
    prompt = (
        "Diagnose the current network state and return the final structured diagnosis.\n\n"
        f"CANONICAL_OBSERVATION: {json.dumps(observation, ensure_ascii=False, sort_keys=True)}\n\n"
        "Requirements:\n"
        "1) Start with topology/Pingmesh-oriented MCP tools before concluding.\n"
        "2) Gather evidence with tools instead of relying on the summary alone.\n"
        "3) If a fault exists, identify fault_type and the most likely network-side location.\n"
        "4) Keep reasoning concise and tied to tool evidence.\n"
        "5) Device logs are valid evidence only inside the observation start_time/end_time. "
        "Pass both timestamps to get_device_logs and ignore entries outside that window.\n"
        "6) If you call an MCP tool, first explain in normal assistant text what evidence you need and why.\n"
        "7) Do not reveal hidden chain-of-thought; keep investigation notes observable and evidence-oriented.\n"
    )
    output = (
        "Return JSON matching DiagnosisOutput with verdict, fault_type, location, evidence, confidence, reasoning. "
    )
    output += "Location must be an object with nullable device and interface. "
    output += "Evidence must be a list of strings. "
    if fenced_output:
        output += "Include exactly one fenced JSON block."
    else:
        output += "Use the configured structured-output schema; do not include Markdown fences."
    return (
        prompt
        + output
        + "\n\nDIAGNOSIS_OUTPUT_SCHEMA: "
        + json.dumps(DiagnosisOutput.model_json_schema(), sort_keys=True)
    )


def build_result(name: str, structured: dict, *, metadata: dict) -> DiagnosisResult:
    output = DiagnosisOutput.model_validate(structured)
    return DiagnosisResult(
        agent_name=name,
        verdict=output.verdict,
        success=True,
        findings={
            "fault_type": output.fault_type,
            "location": output.location.model_dump(),
            "evidence": output.evidence,
        },
        confidence=output.confidence,
        reasoning=output.reasoning,
        metadata=metadata,
    )
