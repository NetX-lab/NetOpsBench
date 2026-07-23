"""Public NetOpsBench SDK exports with lazy loading."""

from __future__ import annotations

_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "NetOpsBench": ("netopsbench.sdk.core", "NetOpsBench"),
    "ScenarioSpec": ("netopsbench.sdk.types", "ScenarioSpec"),
    "EpisodeSpec": ("netopsbench.sdk.types", "EpisodeSpec"),
    "ScenarioManager": ("netopsbench.sdk.scenarios", "ScenarioManager"),
    "supported_scales": ("netopsbench.sdk.scenarios", "supported_scales"),
    "DiagnosticAgent": ("netopsbench.sdk.agents", "DiagnosticAgent"),
    "SyncDiagnosticAgent": ("netopsbench.sdk.agents", "SyncDiagnosticAgent"),
    "AgentHandle": ("netopsbench.sdk.agents", "AgentHandle"),
    "AgentManager": ("netopsbench.sdk.agents", "AgentManager"),
    "AgentTraceRecorder": ("netopsbench.sdk.agents", "AgentTraceRecorder"),
    "DiagnosticContext": ("netopsbench.sdk.types", "DiagnosticContext"),
    "DiagnosisResult": ("netopsbench.sdk.types", "DiagnosisResult"),
    "BuiltinMCPServerHandle": ("netopsbench.sdk.mcp", "BuiltinMCPServerHandle"),
    "builtin_mcp_server_config": ("netopsbench.sdk.mcp", "builtin_mcp_server_config"),
    "builtin_mcp_server_command": ("netopsbench.sdk.mcp", "builtin_mcp_server_command"),
    "start_builtin_mcp_server": ("netopsbench.sdk.mcp", "start_builtin_mcp_server"),
    "FaultContext": ("netopsbench.sdk.types", "FaultContext"),
    "FaultExecutionResult": ("netopsbench.sdk.types", "FaultExecutionResult"),
    "FaultSpec": ("netopsbench.sdk.faults", "FaultSpec"),
    "FaultExecutor": ("netopsbench.sdk.faults", "FaultExecutor"),
    "FaultPack": ("netopsbench.sdk.faults", "FaultPack"),
    "FaultRegistry": ("netopsbench.sdk.faults", "FaultRegistry"),
    "FaultManager": ("netopsbench.sdk.faults", "FaultManager"),
    "simple_fault": ("netopsbench.sdk.faults", "simple_fault"),
    "RuntimeManager": ("netopsbench.sdk.runtimes", "RuntimeManager"),
    "RuntimePool": ("netopsbench.sdk.runtimes", "RuntimePool"),
    "RuntimeIdentity": ("netopsbench.sdk.runtimes", "RuntimeIdentity"),
    "ScaleManager": ("netopsbench.sdk.scales", "ScaleManager"),
    "ScaleProfile": ("netopsbench.sdk.scales", "ScaleProfile"),
    "ScaleRegistry": ("netopsbench.sdk.scales", "ScaleRegistry"),
    "SimulatorManager": ("netopsbench.sdk.simulators", "SimulatorManager"),
    "SimulatorConfig": ("netopsbench.sdk.simulators", "SimulatorConfig"),
    "PreparedIncident": ("netopsbench.sdk.simulators", "PreparedIncident"),
    "DiagnosticSession": ("netopsbench.sdk.simulators", "DiagnosticSession"),
    "CleanupStatus": ("netopsbench.sdk.simulators", "CleanupStatus"),
    "ExecutionFailure": ("netopsbench.sdk.simulators", "ExecutionFailure"),
    "FailureDomain": ("netopsbench.sdk.simulators", "FailureDomain"),
    "IncidentEngine": ("netopsbench.sdk.simulators", "IncidentEngine"),
    "IncidentState": ("netopsbench.sdk.simulators", "IncidentState"),
    "SessionState": ("netopsbench.sdk.simulators", "SessionState"),
    "TerminationReason": ("netopsbench.sdk.simulators", "TerminationReason"),
    "AgentUsage": ("netopsbench.sdk.simulators", "AgentUsage"),
    "DiagnosisLocation": ("netopsbench.sdk.simulators", "DiagnosisLocation"),
    "DiagnosisSubmission": ("netopsbench.sdk.simulators", "DiagnosisSubmission"),
    "ToolAction": ("netopsbench.sdk.simulators", "ToolAction"),
    "SubmitDiagnosisAction": ("netopsbench.sdk.simulators", "SubmitDiagnosisAction"),
    "simulator_tool_schemas": ("netopsbench.sdk.simulators", "simulator_tool_schemas"),
    "validate_tool_action": ("netopsbench.sdk.simulators", "validate_tool_action"),
    "build_canonical_observation": ("netopsbench.sdk.simulators", "build_canonical_observation"),
    "build_public_case_id": ("netopsbench.sdk.simulators", "build_public_case_id"),
    "build_public_symptoms": ("netopsbench.sdk.simulators", "build_public_symptoms"),
    "SessionManager": ("netopsbench.sdk.sessions", "SessionManager"),
    "RunHandle": ("netopsbench.sdk.reports", "RunHandle"),
    "ArtifactManager": ("netopsbench.sdk.artifacts", "ArtifactManager"),
    "BenchmarkReport": ("netopsbench.sdk.reports", "BenchmarkReport"),
    "EvaluatorManager": ("netopsbench.sdk.evaluators", "EvaluatorManager"),
    # -- Exceptions ----------------------------------------------------------
    "NetOpsBenchError": ("netopsbench.sdk.exceptions", "NetOpsBenchError"),
    "ConfigurationError": ("netopsbench.sdk.exceptions", "ConfigurationError"),
    "ScenarioError": ("netopsbench.sdk.exceptions", "ScenarioError"),
    "ScenarioValidationError": ("netopsbench.sdk.exceptions", "ScenarioValidationError"),
    "FaultRegistryError": ("netopsbench.sdk.exceptions", "FaultRegistryError"),
    "FaultNotFoundError": ("netopsbench.sdk.exceptions", "FaultNotFoundError"),
    "FaultValidationError": ("netopsbench.sdk.exceptions", "FaultValidationError"),
    "AgentError": ("netopsbench.sdk.exceptions", "AgentError"),
    "AgentTimeoutError": ("netopsbench.sdk.exceptions", "AgentTimeoutError"),
    "AgentDiagnosisError": ("netopsbench.sdk.exceptions", "AgentDiagnosisError"),
    "RuntimeProvisionError": ("netopsbench.sdk.exceptions", "RuntimeProvisionError"),
    "RunFailedError": ("netopsbench.sdk.exceptions", "RunFailedError"),
}

__all__ = sorted(_EXPORT_MAP)


def __getattr__(name: str):
    if name not in _EXPORT_MAP:
        raise AttributeError(name)
    module_name, attr_name = _EXPORT_MAP[name]
    module = __import__(module_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
