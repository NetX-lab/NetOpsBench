from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from harbor.viewer.scanner import JobScanner

from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.tracing import AgentTraceRecorder
from netopsbench.platform.session.tracing import TraceWriter, export_traces, load_trace_index


@pytest.mark.asyncio
async def test_trace_aware_llm_client_records_private_messages(monkeypatch):
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(type="ai", content="diagnosis draft"))],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
            )

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))
    recorder = AgentTraceRecorder()

    response = await recorder.llm_client("openai", model="gpt-test", api_key="test-key").chat(
        [{"role": "user", "content": "diagnose"}],
        temperature=0,
    )

    assert response.choices[0].message.content == "diagnosis draft"
    assert captured["model"] == "gpt-test"
    assert captured["messages"] == [{"role": "user", "content": "diagnose"}]
    assert recorder.metrics()["input_tokens"] == 7
    assert recorder.metrics()["output_tokens"] == 3
    steps = recorder.to_steps()
    assert [step["message"] for step in steps] == ["diagnosis draft"]
    assert steps[0]["duration_seconds"] is not None
    assert steps[0]["extra"]["llm_request"]["messages"][0]["content"] == "diagnose"


def test_disabled_trace_recorder_preserves_api_without_collecting():
    recorder = AgentTraceRecorder.disabled()
    run_id = recorder.record_llm_request([{"role": "user", "content": "diagnose"}], model="gpt-test")
    recorder.record_llm_response(
        SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(content="ok"))]]), run_id=run_id
    )
    recorder.record_tool_start(name="get_topology", args={"verbose": True}, run_id="tool-1")
    recorder.record_tool_end(output={"ok": True}, run_id="tool-1")
    recorder.record_error(stage="agent", error=RuntimeError("boom"))

    assert recorder.to_steps() == []
    assert recorder.tool_calls() == []
    assert recorder.model_metadata() == {}
    assert recorder.metrics() == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_call_count": 0}
    assert recorder.langchain_callback() is None


def test_trace_recorder_preserves_tool_call_llm_response_payload():
    recorder = AgentTraceRecorder()
    run_id = recorder.record_llm_request(
        [{"role": "user", "content": "inspect topology"}], model="deepseek-v4-pro", provider="deepseek"
    )

    recorder.record_llm_response(
        SimpleNamespace(
            generations=[
                [
                    SimpleNamespace(
                        message=SimpleNamespace(
                            type="ai",
                            content="",
                            tool_calls=[
                                {
                                    "id": "call-1",
                                    "name": "get_topology",
                                    "args": {"verbose": True},
                                    "type": "tool_call",
                                }
                            ],
                            additional_kwargs={"refusal": None},
                            response_metadata={"finish_reason": "tool_calls", "model_name": "deepseek-v4-pro"},
                            usage_metadata={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
                        )
                    )
                ]
            ]
        ),
        run_id=run_id,
    )

    steps = recorder.to_steps()
    assert steps[0]["message"] == ""
    assert steps[0]["provider"] == "deepseek"
    assert steps[0]["model"] == "deepseek-v4-pro"
    assert steps[0]["extra"]["llm_request"]["messages"][0]["content"] == "inspect topology"
    assert steps[0]["extra"]["llm_response"]["content"] == ""
    assert steps[0]["extra"]["llm_response"]["tool_calls"][0]["name"] == "get_topology"
    assert steps[0]["extra"]["llm_response"]["response_metadata"]["finish_reason"] == "tool_calls"
    assert steps[0]["extra"]["llm_response"]["usage_metadata"]["total_tokens"] == 13


def test_trace_recorder_preserves_provider_reasoning_content_when_exposed():
    recorder = AgentTraceRecorder()

    recorder.record_llm_response(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content="final answer",
                        model_extra={"reasoning_content": "provider visible reasoning"},
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12),
        ),
        model="deepseek-v4-pro",
        provider="deepseek",
    )

    step = recorder.to_steps()[0]
    assert step["message"] == "final answer"
    assert step["reasoning_content"] == "provider visible reasoning"
    assert step["extra"]["llm_response"]["reasoning_content"] == "provider visible reasoning"


def test_trace_writer_persists_atif_and_index_with_redaction(tmp_path):
    writer = TraceWriter(tmp_path / "traces", run_id="run-0001")
    context = DiagnosticContext(
        scenario_id="case-123",
        topology={"devices": {"leafs": [{"name": "leaf1"}]}},
        symptoms={"pingmesh_query_window": {"start_time": "2026-01-01T00:00:00Z"}},
        metadata={"OPENAI_API_KEY": "secret", "worker_env": {"NETOPSBENCH_TOKEN": "secret-token"}},
    )
    diagnosis = SimpleNamespace(
        agent_name="agent-x",
        success=True,
        findings={},
        metadata={
            "provider": "openai",
            "model": "gpt-test",
            "runtime": "test-runtime",
            "input_tokens": 10,
            "output_tokens": 3,
            "OPENAI_API_KEY": "secret",
            "worker_env": {"NETOPSBENCH_TOKEN": "secret-token"},
        },
    )
    recorder = AgentTraceRecorder()
    recorder.record_llm_response(
        SimpleNamespace(
            generations=[
                [
                    SimpleNamespace(
                        message=SimpleNamespace(
                            type="ai",
                            content="ok",
                            usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                        )
                    )
                ]
            ]
        ),
        model="gpt-test",
        provider="openai",
    )

    result = writer.write_case_trace(
        case_id="case-123",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo-1",
        runtime_id="runtime-1",
        topology_scale="small",
        agent=SimpleNamespace(name="agent-x"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "fault_detected", "metadata": diagnosis.metadata},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
        pingmesh_window={"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T00:01:00Z"},
        trace_recorder=recorder,
    )

    atif = json.loads((tmp_path / "traces" / "worker-1" / "case-123" / "trajectory.atif.json").read_text())
    index = load_trace_index(tmp_path)

    assert result.atif_path.endswith("trajectory.atif.json")
    assert not (tmp_path / "traces" / "worker-1" / "case-123" / "trace.json").exists()
    assert atif["schema_version"] == "ATIF-v1.7"
    assert atif["trajectory_id"] == result.trace_id
    assert atif["steps"][0]["step_id"] == 1
    assert atif["steps"][0]["source"] == "user"
    assert atif["steps"][1]["source"] == "agent"
    assert atif["steps"][1]["message"] == "ok"
    assert atif["steps"][2]["message"].startswith("Final diagnosis:")
    assert atif["extra"]["final_diagnosis"]["metadata"]["OPENAI_API_KEY"] == "<redacted>"
    assert atif["extra"]["final_diagnosis"]["metadata"]["worker_env"]["NETOPSBENCH_TOKEN"] == "<redacted>"
    assert atif["extra"]["topology_scale"] == "small"
    assert atif["final_metrics"]["total_prompt_tokens"] == 10
    assert index[0]["case_id"] == "case-123"
    assert index[0]["status"] == "completed"
    assert index[0]["provider"] == "openai"
    assert index[0]["model"] == "gpt-test"
    assert index[0]["topology_scale"] == "small"
    assert index[0]["started_at"] == "2026-01-01T00:00:00+00:00"
    assert index[0]["duration_seconds"] == 2.0


def test_trace_writer_falls_back_to_agent_display_metadata_on_early_failure(tmp_path):
    writer = TraceWriter(tmp_path / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-123", topology={}, symptoms={})
    agent = SimpleNamespace(name="agent-x", vendor="deepseek", model="deepseek-v4-pro")
    diagnosis = SimpleNamespace(agent_name="agent-x", success=False, findings={"error": "missing key"}, metadata={})

    writer.write_case_trace(
        case_id="case-123",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo-1",
        runtime_id="runtime-1",
        topology_scale="small",
        agent=agent,
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"error": "missing key", "success": False, "metadata": {}},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        error="missing key",
    )

    atif = json.loads((tmp_path / "traces" / "worker-1" / "case-123" / "trajectory.atif.json").read_text())
    index = load_trace_index(tmp_path)

    assert index[0]["provider"] == "deepseek"
    assert index[0]["model"] == "deepseek-v4-pro"
    assert index[0]["topology_scale"] == "small"
    assert atif["agent"]["model_name"] == "deepseek-v4-pro"
    assert atif["agent"]["extra"]["provider"] == "deepseek"


def test_export_traces_writes_harbor_jobs_directory(tmp_path):
    writer = TraceWriter(tmp_path / "run-0001" / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-1", topology={}, symptoms={})
    diagnosis = SimpleNamespace(
        agent_name="agent",
        success=True,
        findings={},
        metadata={"provider": "openai", "model": "gpt-test", "input_tokens": 5, "output_tokens": 2},
    )
    trace_result = writer.write_case_trace(
        case_id="case-1",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        topology_scale="small",
        agent=SimpleNamespace(name="agent"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "network_healthy", "metadata": {}},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    scenario_result = {
        "episodes": [
            {
                "episode": {"episode_id": "ep1"},
                "diagnosis": {
                    "trace": {
                        "trace_id": trace_result.trace_id,
                        "case_id": trace_result.case_id,
                        "atif_path": trace_result.atif_path,
                    }
                },
            }
        ]
    }
    writer.write_evaluation_results(
        evaluation_results=[
            SimpleNamespace(
                to_dict=lambda: {
                    "testcase_id": "scenario-1:ep1",
                    "score": 1.0,
                    "details": {"scenario_id": "scenario-1", "episode_id": "ep1"},
                }
            )
        ],
        scenario_result=scenario_result,
    )
    (tmp_path / "run-0001" / "report.json").write_text(
        '{"summary":{"started_at":"2026-01-01T00:00:00Z","completed_at":"2026-01-01T00:00:01Z"}}',
        encoding="utf-8",
    )

    output = export_traces(tmp_path / "run-0001", output=tmp_path / "harbor-jobs")
    job_dir = output / "netopsbench-run-0001"
    trial_dir = job_dir / "scenario-1__case-1"

    job_result = json.loads((job_dir / "result.json").read_text())
    job_config = json.loads((job_dir / "config.json").read_text())
    trial_result = json.loads((trial_dir / "result.json").read_text())
    trajectory = json.loads((trial_dir / "agent" / "trajectory.json").read_text())
    verifier = json.loads((trial_dir / "verifier" / "result.json").read_text())

    assert job_result["n_total_trials"] == 1
    assert job_result["stats"]["n_completed_trials"] == 1
    assert job_result["trial_results"][0]["trial_name"] == "scenario-1__case-1"
    eval_stats = next(iter(job_result["stats"]["evals"].values()))
    assert eval_stats["metrics"][0]["reward"] == 1.0
    assert eval_stats["metrics"][0]["score"] == 1.0
    assert job_config["datasets"][0]["name"] == "netopsbench-small"
    assert job_config["agents"][0]["name"] == "agent"
    assert job_config["agents"][0]["model_name"] == "openai/gpt-test"
    assert trial_result["task_name"] == "scenario-1"
    assert trial_result["source"] == "netopsbench-small"
    assert trial_result["config"]["task"]["source"] == "netopsbench-small"
    assert trial_result["agent_info"]["model_info"]["provider"] == "openai"
    assert trial_result["agent_result"]["n_input_tokens"] == 5
    assert verifier["reward"] == 1.0
    assert trajectory["schema_version"] == "ATIF-v1.7"
    scanner = JobScanner(output)
    job_result_model = scanner.get_job_result("netopsbench-run-0001")
    trial_result_model = scanner.get_trial_result("netopsbench-run-0001", "scenario-1__case-1")
    assert job_result_model is not None
    assert job_result_model.n_total_trials == 1
    assert trial_result_model is not None
    assert trial_result_model.verifier_result.rewards == {"reward": 1.0, "score": 1.0}


def test_atif_builds_steps_from_trace_recorder_events(tmp_path):
    writer = TraceWriter(tmp_path / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-123", topology={}, symptoms={})
    diagnosis = SimpleNamespace(
        agent_name="agent-x",
        success=True,
        findings={},
        metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )
    recorder = AgentTraceRecorder()
    recorder.record_llm_request(
        [
            SimpleNamespace(type="system", content="system prompt"),
            SimpleNamespace(type="human", content="diagnose"),
        ],
        run_id="llm-1",
        model="gpt-test",
        provider="openai",
    )
    recorder.record_tool_start(name="get_topology", args={"verbose": True}, run_id="call-1", parent_run_id="llm-1")
    recorder.record_tool_end(output={"ok": True}, run_id="call-1")
    recorder.record_llm_response(
        SimpleNamespace(
            generations=[
                [
                    SimpleNamespace(
                        message=SimpleNamespace(
                            type="ai",
                            content="done",
                            usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                        )
                    )
                ]
            ]
        ),
        run_id="llm-1",
        model="gpt-test",
        provider="openai",
    )

    writer.write_case_trace(
        case_id="case-123",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        agent=SimpleNamespace(name="agent-x"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "network_healthy", "metadata": diagnosis.metadata},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        trace_recorder=recorder,
    )

    atif = json.loads((tmp_path / "traces" / "worker-1" / "case-123" / "trajectory.atif.json").read_text())

    assert [step["source"] for step in atif["steps"]] == ["user", "agent", "agent", "agent"]
    assert atif["steps"][1]["tool_calls"][0]["function_name"] == "get_topology"
    assert atif["steps"][1]["tool_calls"][0]["arguments"] == {"verbose": True}
    assert atif["steps"][1]["observation"]["results"][0]["content"] == '{"ok": true}'
    assert atif["steps"][2]["message"] == "done"
    assert atif["steps"][2]["extra"]["llm_request"]["messages"][0]["content"] == "system prompt"
    assert atif["steps"][2]["extra"]["llm_request"]["messages"][1]["content"] == "diagnose"
    assert atif["steps"][2]["metrics"]["prompt_tokens"] == 4
    assert "duration_seconds" in atif["steps"][2]["metrics"]["extra"]
    assert atif["steps"][3]["message"].startswith("Final diagnosis:")


def test_trace_writer_ignores_manual_metadata_trace_payloads(tmp_path):
    writer = TraceWriter(tmp_path / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-123", topology={}, symptoms={})
    diagnosis = SimpleNamespace(
        agent_name="agent-x",
        success=True,
        findings={},
        metadata={
            "trace": {
                "steps": [{"type": "llm", "message": "manual trace should be ignored"}],
            }
        },
    )
    recorder = AgentTraceRecorder()
    recorder.record_llm_response(
        SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(type="ai", content="recorder wins"))]]),
        model="gpt-test",
    )

    writer.write_case_trace(
        case_id="case-123",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        agent=SimpleNamespace(name="agent-x"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "network_healthy", "metadata": diagnosis.metadata},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        trace_recorder=recorder,
    )

    atif = json.loads((tmp_path / "traces" / "worker-1" / "case-123" / "trajectory.atif.json").read_text())

    assert "manual trace should be ignored" not in json.dumps(atif)
    assert any(step.get("message") == "recorder wins" for step in atif["steps"])


def test_trace_writer_falls_back_to_tool_calls_without_trace(tmp_path):
    writer = TraceWriter(tmp_path / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-123", topology={}, symptoms={})
    diagnosis = SimpleNamespace(
        agent_name="agent-x",
        success=True,
        findings={},
        metadata={"tool_calls": [{"tool": "get_pingmesh_summary", "args": {"time_range_minutes": 5}}]},
    )

    writer.write_case_trace(
        case_id="case-123",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        agent=SimpleNamespace(name="agent-x"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "network_healthy", "metadata": diagnosis.metadata},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    atif = json.loads((tmp_path / "traces" / "worker-1" / "case-123" / "trajectory.atif.json").read_text())

    assert len(atif["steps"]) == 3
    assert atif["steps"][1]["tool_calls"][0]["function_name"] == "get_pingmesh_summary"
    assert atif["steps"][1]["tool_calls"][0]["arguments"] == {"time_range_minutes": 5}
    assert atif["steps"][2]["message"].startswith("Final diagnosis:")


def test_trace_writer_persists_evaluation_results_and_failures(tmp_path):
    writer = TraceWriter(tmp_path / "run-0001" / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-1", topology={}, symptoms={})
    diagnosis = SimpleNamespace(agent_name="agent", success=True, findings={}, metadata={"tool_calls": []})
    trace_result = writer.write_case_trace(
        case_id="case-1",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        agent=SimpleNamespace(name="agent"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"verdict": "network_healthy", "metadata": {}, "trace": {}},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    scenario_result = {
        "episodes": [
            {
                "episode": {"episode_id": "ep1"},
                "diagnosis": {
                    "trace": {
                        "trace_id": trace_result.trace_id,
                        "case_id": trace_result.case_id,
                        "atif_path": trace_result.atif_path,
                    }
                },
            }
        ]
    }
    writer.write_evaluation_results(
        evaluation_results=[
            SimpleNamespace(
                to_dict=lambda: {
                    "testcase_id": "scenario-1:ep1",
                    "score": 1.0,
                    "correct_verdict": True,
                    "correct_device": True,
                    "correct_interface": True,
                    "correct_fault_type": True,
                    "details": {"scenario_id": "scenario-1", "episode_id": "ep1", "ground_truth": {}},
                }
            )
        ],
        scenario_result=scenario_result,
    )
    writer.write_failure_result(
        scenario_id="scenario-1",
        scenario_result=scenario_result,
        stage="evaluator",
        error=RuntimeError("judge failed"),
    )
    rows = [json.loads(line) for line in (tmp_path / "run-0001" / "traces" / "results.jsonl").read_text().splitlines()]
    assert rows[0]["trace_id"] == trace_result.trace_id
    assert rows[0]["case_id"] == "case-1"
    assert rows[0]["details"]["ground_truth"] == {}
    assert rows[1]["error_stage"] == "evaluator"
    assert rows[1]["error_type"] == "RuntimeError"


def test_trace_writer_links_results_from_index_when_raw_diagnosis_lacks_trace(tmp_path):
    writer = TraceWriter(tmp_path / "run-0001" / "traces", run_id="run-0001")
    context = DiagnosticContext(scenario_id="case-1", topology={}, symptoms={})
    diagnosis = SimpleNamespace(agent_name="agent", success=False, findings={"error": "boom"}, metadata={})
    trace_result = writer.write_case_trace(
        case_id="case-1",
        scenario_id="scenario-1",
        episode_result={"episode": {"episode_id": "ep1"}},
        worker="worker-1",
        topology_id="topo",
        runtime_id="runtime",
        agent=SimpleNamespace(name="agent"),
        diagnostic_context=context,
        diagnosis=diagnosis,
        diagnosis_payload={"error": "boom", "success": False, "metadata": {}},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ended_at=datetime(2026, 1, 1, tzinfo=UTC),
        error="boom",
    )
    scenario_result = {
        "episodes": [
            {
                "episode": {"episode_id": "ep1"},
                "diagnosis": {"error": "boom", "success": False},
            }
        ]
    }

    writer.write_evaluation_results(
        evaluation_results=[
            SimpleNamespace(
                to_dict=lambda: {
                    "testcase_id": "scenario-1:ep1",
                    "score": 0.0,
                    "details": {"scenario_id": "scenario-1", "episode_id": "ep1"},
                }
            )
        ],
        scenario_result=scenario_result,
    )

    rows = [json.loads(line) for line in (tmp_path / "run-0001" / "traces" / "results.jsonl").read_text().splitlines()]
    assert rows[0]["trace_id"] == trace_result.trace_id
    assert rows[0]["case_id"] == "case-1"
    assert rows[0]["atif_path"] == trace_result.atif_path
