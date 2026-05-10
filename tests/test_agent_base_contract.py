"""Public contract tests for the slim agent base module."""

from netopsbench.agents.base import VALID_AGENT_VERDICTS, DiagnosticContext


def test_public_agent_verdict_contract_exports_expected_values():
    assert VALID_AGENT_VERDICTS == ("fault_detected", "network_healthy", "inconclusive")
    assert "network_healthy" in VALID_AGENT_VERDICTS
    assert "no_fault" not in VALID_AGENT_VERDICTS


def test_diagnostic_context_exposes_public_agent_input_fields():
    context = DiagnosticContext(
        scenario_id="example",
        topology={"name": "dcn"},
        symptoms={"observations": {}},
        tools="toolkit",
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": "/tmp/topology"}},
    )

    assert context.scenario_id == "example"
    assert context.topology == {"name": "dcn"}
    assert context.symptoms == {"observations": {}}
    assert context.tools == "toolkit"
    assert context.metadata["worker_env"]["NETOPSBENCH_TOPOLOGY_DIR"] == "/tmp/topology"
