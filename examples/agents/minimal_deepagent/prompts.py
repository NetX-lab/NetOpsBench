"""Prompt helpers for the public DeepAgent example."""

from __future__ import annotations

import json
from typing import Any

from netopsbench.platform.session.context import build_canonical_observation

DEFAULT_SYSTEM_PROMPT = (
    "You are a production network troubleshooting expert for DCN fabrics. "
    "Use NetOpsBench MCP tools to diagnose live issues with evidence-first reasoning. "
    "Do not assume hidden ground truth. "
    "The DiagnosisOutput.verdict field is a strict enum — it MUST be exactly one of "
    "'fault_detected', 'network_healthy', or 'inconclusive'. "
    "Do NOT use synonyms such as 'fault', 'fault_found', 'fault_confirmed', or 'fault_resolved' — "
    "any of those will be scored as wrong. "
    "If evidence is insufficient, return verdict='inconclusive' with an empty location. "
    "Prefer Pingmesh and topology tools first, then validate with interface, routing, and log evidence. "
    "Be efficient: avoid redundant tool calls and do not repeat the same query. "
    "You have a limited tool-call budget — focus on the most informative tools first. "
    "When your investigation is complete, return a final answer containing one fenced JSON block that matches "
    "the DiagnosisOutput schema."
)


def build_user_prompt(context: Any) -> str:
    metadata = getattr(context, "metadata", {}) or {}
    observation = metadata.get("canonical_observation")
    if not isinstance(observation, dict):
        observation = build_canonical_observation(
            case_id=str(getattr(context, "scenario_id", "unknown")),
            topology=dict(getattr(context, "topology", {}) or {}),
            symptoms=dict(getattr(context, "symptoms", {}) or {}),
        )
    return (
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
        "8) When done, include exactly one fenced ```json block with fields: "
        "verdict, fault_type, location, evidence, confidence, reasoning.\n"
        "9) The JSON verdict MUST be exactly one of fault_detected, network_healthy, or inconclusive.\n"
        "10) The JSON location MUST be an object, never a string: "
        '{"device": "leaf1", "interface": "Ethernet8"}. '
        'Use {"device": null, "interface": null} when location is unknown or not applicable.\n'
        "11) Final JSON shape example:\n"
        "```json\n"
        '{"verdict":"fault_detected","fault_type":"link_down",'
        '"location":{"device":"leaf1","interface":"Ethernet8"},'
        '"evidence":["brief tool-backed fact"],"confidence":0.8,'
        '"reasoning":"short evidence-based summary"}\n'
        "```\n"
    )
