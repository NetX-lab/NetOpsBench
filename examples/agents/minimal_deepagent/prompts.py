"""Prompt helpers for the public DeepAgent example."""

from __future__ import annotations

import json
from typing import Any, Dict, List

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
    "When your investigation is complete, you MUST call the DiagnosisOutput tool to submit your final structured diagnosis."
)


def build_context_summary(context: Any) -> Dict[str, Any]:
    topology = context.topology if isinstance(getattr(context, "topology", None), dict) else {}
    symptoms = context.symptoms if isinstance(getattr(context, "symptoms", None), dict) else {}
    devices = topology.get("devices") or {}
    observations = symptoms.get("observations") or {}
    pingmesh_metrics = observations.get("pingmesh_metrics") or {}

    return {
        "scenario_id": getattr(context, "scenario_id", "unknown"),
        "topology_counts": {
            "spines": len(devices.get("spines") or []),
            "leafs": len(devices.get("leafs") or []),
            "clients": len(devices.get("clients") or []),
            "links": len(topology.get("links") or []),
        },
        "symptoms_keys": sorted(symptoms.keys()),
        "observations_keys": sorted(observations.keys()),
        "episode": symptoms.get("episode") or {},
        "pingmesh_query_window": symptoms.get("pingmesh_query_window") or {},
        "pingmesh_summary": (pingmesh_metrics.get("summary") or {}) if isinstance(pingmesh_metrics, dict) else {},
    }



def build_compact_anomalies(context: Any, limit: int = 6) -> List[Dict[str, Any]]:
    symptoms = context.symptoms if isinstance(getattr(context, "symptoms", None), dict) else {}
    observations = symptoms.get("observations") or {}
    pingmesh_metrics = observations.get("pingmesh_metrics") or {}
    anomalies = pingmesh_metrics.get("anomalies") or []

    compact: List[Dict[str, Any]] = []
    for item in anomalies[:limit]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "type": item.get("type"),
                "src_name": item.get("src_name") or item.get("src"),
                "dst_name": item.get("dst_name") or item.get("dst"),
                "src_leaf": item.get("src_leaf"),
                "dst_leaf": item.get("dst_leaf"),
                "severity": item.get("severity"),
                "window": item.get("observation_window"),
                "value": item.get("value") or item.get("loss_pct") or item.get("current_loss"),
            }
        )
    return compact



def build_user_prompt(context: Any) -> str:
    summary = build_context_summary(context)
    anomalies = build_compact_anomalies(context)
    return (
        "Diagnose the current network state and return the final structured diagnosis.\n\n"
        f"SCENARIO_SUMMARY: {json.dumps(summary, ensure_ascii=False)}\n"
        f"OBSERVED_ANOMALIES: {json.dumps(anomalies, ensure_ascii=False)}\n\n"
        "Requirements:\n"
        "1) Start with topology/Pingmesh-oriented MCP tools before concluding.\n"
        "2) Gather evidence with tools instead of relying on the summary alone.\n"
        "3) If a fault exists, identify fault_type and the most likely network-side location.\n"
        "4) Keep reasoning concise and tied to tool evidence.\n"
        "5) When done, call the DiagnosisOutput tool with your final structured diagnosis.\n"
    )
