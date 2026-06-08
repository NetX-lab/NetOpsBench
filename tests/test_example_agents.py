"""Tests for the public example DeepAgent implementation."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

# Stub third-party agent deps so the module can be imported without them.
for _mod in [
    "deepagents",
    "deepagents.backends",
    "langchain_mcp_adapters",
    "langchain_mcp_adapters.sessions",
    "langchain_mcp_adapters.tools",
    "langchain_openai",
]:
    sys.modules.setdefault(_mod, MagicMock())

from examples.agents import MinimalDeepAgent  # noqa: E402
from examples.agents.minimal_deepagent.providers import get_provider  # noqa: E402
from netopsbench.agents.base import DiagnosticContext  # noqa: E402
from netopsbench.agents.tracing import AgentTraceRecorder  # noqa: E402
from netopsbench.sdk.agents import DiagnosisResult  # noqa: E402


class _FakeRuntime:
    """Fake DeepAgent graph that captures invoke calls."""

    def __init__(self, result):
        self.result = result
        self.prompts = []
        self.configs = []

    async def ainvoke(self, payload, config=None):
        prompt = payload["messages"][0]["content"] if payload.get("messages") else ""
        self.prompts.append(prompt)
        self.configs.append(config or {})
        return self.result


class _FailingRuntime:
    """Fake graph that emits callback events before failing."""

    def __init__(self, exc):
        self.exc = exc
        self.prompts = []
        self.configs = []

    async def ainvoke(self, payload, config=None):
        prompt = payload["messages"][0]["content"] if payload.get("messages") else ""
        self.prompts.append(prompt)
        self.configs.append(config or {})

        for callback in (config or {}).get("callbacks", []):
            callback.on_tool_start({"name": "get_pingmesh_hotspots"}, "{}", run_id=uuid4())
            callback.on_tool_start({"name": "get_device_interfaces"}, "{}", run_id=uuid4())
            callback.on_llm_end(
                SimpleNamespace(
                    generations=[
                        [
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    usage_metadata={
                                        "input_tokens": 17,
                                        "output_tokens": 6,
                                        "total_tokens": 23,
                                    }
                                )
                            )
                        ]
                    ]
                ),
                run_id=uuid4(),
            )

        raise self.exc


def _patch_agent_deps(monkeypatch, fake_graph):
    """Monkeypatch the module-level DeepAgent dependencies in agent.py."""
    import examples.agents.minimal_deepagent.agent as agent_mod
    import examples.agents.minimal_deepagent.providers.runtime as provider_runtime

    async def _fake_connect_mcp_tools(exit_stack, server_config):
        return []

    monkeypatch.setattr(agent_mod, "_connect_mcp_tools", _fake_connect_mcp_tools)
    monkeypatch.setattr(provider_runtime, "_connect_mcp_tools", _fake_connect_mcp_tools)
    monkeypatch.setattr(agent_mod, "FilesystemBackend", lambda **kw: None)
    monkeypatch.setattr(agent_mod, "create_deep_agent", lambda **kw: fake_graph)


def _diagnosis_json_message(payload=None, **message_kwargs):
    structured = {
        "verdict": "inconclusive",
        "fault_type": None,
        "location": {"device": None, "interface": None},
        "evidence": [],
        "confidence": 0.0,
        "reasoning": "",
    }
    if payload:
        structured.update(payload)
    content = "```json\n" + json.dumps(structured) + "\n```"
    return SimpleNamespace(type="ai", content=content, **message_kwargs)


def test_minimal_deepagent_diagnose_returns_public_diagnosis_result(tmp_path, monkeypatch):
    raw_result = {
        "messages": [
            SimpleNamespace(type="ai", usage_metadata={"input_tokens": 21, "output_tokens": 7, "total_tokens": 28}),
            SimpleNamespace(type="tool", name="get_pingmesh_hotspots", content={"ok": True}),
            SimpleNamespace(type="tool", name="get_device_interfaces", content={"ok": True}),
            _diagnosis_json_message(
                {
                    "verdict": "fault_detected",
                    "fault_type": "link_down",
                    "location": {"device": "leaf-01", "interface": "Ethernet1"},
                    "evidence": ["pingmesh anomaly", "interface down"],
                    "confidence": 0.91,
                    "reasoning": "Pingmesh hotspots aligned with an interface-down observation.",
                },
                response_metadata={"token_usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
            ),
        ],
    }
    fake_graph = _FakeRuntime(raw_result)

    agent = MinimalDeepAgent(
        name="example-deepagent",
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(
        scenario_id="scenario-001",
        topology={"devices": {"leafs": [{"name": "leaf-01"}]}, "links": []},
        symptoms={"observations": {"pingmesh_metrics": {"anomalies": [{"type": "packet_loss"}]}}},
        trace=SimpleNamespace(langchain_callback=lambda: "trace-callback"),
    )

    result = asyncio.run(agent.diagnose(context))

    assert isinstance(result, DiagnosisResult)
    assert result.agent_name == "example-deepagent"
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "link_down"
    assert result.findings["location"]["device"] == "leaf-01"
    assert len(result.metadata["tool_calls"]) == 2
    assert result.metadata["input_tokens"] == 30
    assert result.metadata["output_tokens"] == 11
    assert result.metadata["total_tokens"] == 41
    assert result.metadata["llm_call_count"] == 2
    assert "trace" not in result.metadata
    assert fake_graph.configs[0]["callbacks"] == ["trace-callback"]
    assert fake_graph.prompts and "SCENARIO_SUMMARY" in fake_graph.prompts[0]


def test_minimal_deepagent_prompt_does_not_include_ground_truth(tmp_path, monkeypatch):
    fake_graph = _FakeRuntime({"messages": [_diagnosis_json_message()]})
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(
        scenario_id="scenario-gt",
        topology={"devices": {}, "links": []},
        symptoms={"observations": {"pingmesh_metrics": {"anomalies": []}}},
        ground_truth={"fault_type": "secret_truth", "location": {"device": "secret-device"}},
    )

    asyncio.run(agent.diagnose(context))

    prompt = fake_graph.prompts[0]
    assert "ground_truth" not in prompt
    assert "secret_truth" not in prompt
    assert "secret-device" not in prompt


def test_minimal_deepagent_reads_minimax_api_key_from_environment(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "shell-env-key")

    agent = MinimalDeepAgent()

    assert agent.api_key == "shell-env-key"
    assert agent.model == "MiniMax-M3"


def test_minimal_deepagent_openai_defaults_and_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-shell-key")

    agent = MinimalDeepAgent(vendor="openai")

    assert agent.api_key == "openai-shell-key"
    assert agent.model == "gpt-5.4"
    assert agent.base_url == "https://api.openai.com/v1"


def test_minimal_deepagent_canonical_module_path():
    assert MinimalDeepAgent.__module__ == "examples.agents.minimal_deepagent.agent"


def test_minimal_deepagent_supports_kimi_vendor(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kimi-test-key")

    provider = get_provider("kimi")
    agent = MinimalDeepAgent(vendor="kimi")

    assert provider.PRESET["model"] == "kimi-k2.6"
    assert provider.PRESET["api_key_env"] == "KIMI_API_KEY"
    assert agent.model == "kimi-k2.6"
    assert agent.base_url == "https://api.moonshot.cn/v1"
    assert agent.api_key == "kimi-test-key"


def test_minimal_deepagent_does_not_expose_legacy_process_method():
    agent = MinimalDeepAgent(api_key="test-key")

    assert not hasattr(agent, "process")


def test_minimal_deepagent_closes_exit_stack_within_each_diagnose_call(tmp_path, monkeypatch):
    fake_graph = _FakeRuntime({"messages": [_diagnosis_json_message()]})
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(
        scenario_id="scenario-close",
        topology={"devices": {}},
        symptoms={"observations": {"pingmesh_metrics": {"anomalies": []}}},
    )

    result = asyncio.run(agent.diagnose(context))
    assert result.verdict == "inconclusive"


def test_minimal_deepagent_parses_final_json_block(monkeypatch):
    fake_graph = _FakeRuntime(
        {
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content=(
                        "```json\n"
                        '{"verdict":"fault_detected","fault_type":"link_down",'
                        '"location":{"device":"leaf1","interface":"Ethernet8"}}\n'
                        "```"
                    ),
                )
            ]
        }
    )
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(scenario_id="scenario-json-block", topology={"devices": {}}, symptoms={})

    result = asyncio.run(agent.diagnose(context))

    assert result.success is True
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "link_down"
    assert result.findings["location"]["device"] == "leaf1"
    assert result.findings["location"]["interface"] == "Ethernet8"
    assert result.metadata["tool_calls"] == []
    assert result.metadata["input_tokens"] == 0


def test_minimal_deepagent_rejects_schema_invalid_final_json(monkeypatch):
    fake_graph = _FakeRuntime(
        {
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content='```json\n{"verdict":"fault_detected","fault_type":"link_down","location":"leaf1"}\n```',
                )
            ]
        }
    )
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(scenario_id="scenario-invalid-json-schema", topology={"devices": {}}, symptoms={})
    result = asyncio.run(agent.diagnose(context))

    assert result.success is False
    assert result.verdict == "inconclusive"
    assert "JSON block missing or invalid" in result.findings["error"]
    assert result.metadata["error_type"] == "ValueError"


def test_minimal_deepagent_uses_json_repair_fallback_for_final_json(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "json_repair",
        SimpleNamespace(
            loads=lambda text: {
                "verdict": "fault_detected",
                "fault_type": "link_down",
                "location": {"device": "leaf1", "interface": "Ethernet8"},
                "evidence": ["repaired"],
                "confidence": 0.8,
                "reasoning": "repaired malformed JSON",
            }
        ),
    )
    fake_graph = _FakeRuntime(
        {
            "messages": [
                SimpleNamespace(
                    type="ai",
                    content='```json\n{"verdict":"fault_detected","fault_type":"link_down",}\n```',
                )
            ]
        }
    )
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(scenario_id="scenario-repair-json", topology={"devices": {}}, symptoms={})
    result = asyncio.run(agent.diagnose(context))

    assert result.success is True
    assert result.verdict == "fault_detected"
    assert result.findings["location"]["device"] == "leaf1"
    assert result.reasoning == "repaired malformed JSON"


def test_minimal_deepagent_tool_call_extraction(monkeypatch):
    """Tool calls from messages are extracted into metadata."""
    fake_graph = _FakeRuntime(
        {
            "messages": [
                SimpleNamespace(type="tool", name="get_topology", content={"ok": True}),
                _diagnosis_json_message(),
            ],
        }
    )
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    context = DiagnosticContext(scenario_id="scenario-tools", topology={"devices": {}}, symptoms={})
    result = asyncio.run(agent.diagnose(context))

    assert len(result.metadata["tool_calls"]) == 1
    assert result.metadata["tool_calls"][0]["tool"] == "get_topology"


def test_minimal_deepagent_preserves_runtime_usage_on_agent_error(monkeypatch):
    fake_graph = _FailingRuntime(RuntimeError("recursion-like failure"))
    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    _patch_agent_deps(monkeypatch, fake_graph)

    recorder = AgentTraceRecorder()
    context = DiagnosticContext(
        scenario_id="scenario-runtime-error",
        topology={"devices": {}},
        symptoms={},
        trace=recorder,
    )

    result = asyncio.run(agent.diagnose(context))

    assert result.success is False
    assert result.verdict == "inconclusive"
    assert result.metadata["error_type"] == "RuntimeError"
    assert recorder.metrics()["input_tokens"] == 17
    assert recorder.metrics()["output_tokens"] == 6
    assert recorder.metrics()["total_tokens"] == 23
    assert recorder.metrics()["llm_call_count"] == 1
    assert [call["tool"] for call in recorder.tool_calls()] == [
        "get_pingmesh_hotspots",
        "get_device_interfaces",
    ]


def test_minimal_deepagent_aclose_is_safe_without_persistent_runtime():
    agent = MinimalDeepAgent(api_key="test-key")

    asyncio.run(agent.aclose())


class _FakeReport:
    def __init__(self):
        self.summary = {"status": "completed"}
        self.scenario_summaries = [{"scenario_id": "demo"}]
        self.artifact_paths = {"report": "/tmp/report.json"}

    def pretty_print(self):
        return None


class _FakeRun:
    def __init__(self):
        self.id = "run-001"
        self.runtime_id = "runtime-001"
        self.status = "completed"

    def wait(self, raise_on_failure=False):
        return _FakeReport()


class _FakeRuntimePool:
    def __init__(self):
        self.name = "runtime-001"
        self.state = "ready"
        self.size = 2
        self.workers = [
            SimpleNamespace(
                name="worker-1", lab_name="lab-1", topology_dir="/tmp/topology-1", mgmt_subnet="10.0.0.0/24"
            ),
            SimpleNamespace(
                name="worker-2", lab_name="lab-2", topology_dir="/tmp/topology-2", mgmt_subnet="10.0.1.0/24"
            ),
        ]
        self.teardown_called = False

    def teardown(self):
        self.teardown_called = True
        self.state = "deleted"


class _FakeRuntimes:
    def __init__(self):
        self.calls = []
        self.runtime = _FakeRuntimePool()

    def provision(self, **kwargs):
        self.calls.append(kwargs)
        return self.runtime


class _FakeSessions:
    def __init__(self):
        self.calls = []
        self.run = _FakeRun()

    def run_scenario(self, **kwargs):
        self.calls.append(("scenario", kwargs))
        return self.run

    def run_suite(self, **kwargs):
        self.calls.append(("suite", kwargs))
        return self.run

    def run_on_runtime_scenario(self, **kwargs):
        self.calls.append(("on_runtime_scenario", kwargs))
        return self.run

    def run_on_runtime_suite(self, **kwargs):
        self.calls.append(("on_runtime_suite", kwargs))
        return self.run


class _FakeFaults:
    def register_pack(self, pack):
        pass


class _FakeBench:
    def __init__(self, workspace):
        self.workspace = workspace
        self.runtimes = _FakeRuntimes()
        self.sessions = _FakeSessions()
        self.faults = _FakeFaults()
        self.agents = SimpleNamespace(wrap=lambda agent: agent)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeAgent:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _prepare_basic_repo(tmp_path):
    repo = tmp_path / "repo"
    scenario_root = repo / "scenarios" / "generated" / "xs"
    scenario_root.mkdir(parents=True)
    (scenario_root / "generated_link_down_xs_001.yaml").write_text("scenario: link_down\n", encoding="utf-8")
    (scenario_root / "generated_packet_loss_xs_001.yaml").write_text("scenario: packet_loss\n", encoding="utf-8")
    (scenario_root / "generated_high_latency_xs_001.yaml").write_text("scenario: high_latency\n", encoding="utf-8")
    # Also create custom fault scenario for 03_custom_faults
    fault_dir = repo / "examples" / "faults" / "custom_fault_pack"
    fault_dir.mkdir(parents=True)
    (fault_dir / "scenario.yaml").write_text("scenario: custom\n", encoding="utf-8")
    return repo


def test_01_run_scenario_main(tmp_path):
    import importlib

    mod = importlib.import_module("examples.01_run_scenario")
    repo = _prepare_basic_repo(tmp_path)

    result = mod.main(repo_root=repo, bench_cls=_FakeBench, agent_cls=_FakeAgent)

    assert result == 0


def test_02_run_suite_main(tmp_path):
    import importlib

    mod = importlib.import_module("examples.02_run_suite")
    repo = _prepare_basic_repo(tmp_path)

    result = mod.main(repo_root=repo, bench_cls=_FakeBench, agent_cls=_FakeAgent)

    assert result == 0


def test_03_run_scale_benchmark_main(tmp_path):
    import importlib

    mod = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_basic_repo(tmp_path)

    result = mod.main(repo_root=repo, bench_cls=_FakeBench, agent_cls=_FakeAgent)

    assert result == 0


def test_04_custom_faults_main(tmp_path):
    import importlib

    mod = importlib.import_module("examples.04_custom_faults")
    repo = _prepare_basic_repo(tmp_path)

    result = mod.main(repo_root=repo, bench_cls=_FakeBench, agent_cls=_FakeAgent)

    assert result == 0


def test_05_manual_runtime_main(tmp_path, capsys):
    import importlib

    mod = importlib.import_module("examples.05_manual_runtime")
    repo = _prepare_basic_repo(tmp_path)

    result = mod.main(repo_root=repo, bench_cls=_FakeBench, agent_cls=_FakeAgent)

    assert result == 0
    output = capsys.readouterr().out
    assert "Tear it down when finished:" in output
    assert "PYTHONPATH=. netopsbench runtime teardown manual-" in output


def test_example_common_builds_wrapped_agent_with_vendor_fallback():
    from examples._common import build_wrapped_agent

    class _NoVendorAgent:
        def __init__(self):
            self.name = "no-vendor"

    bench = _FakeBench(workspace=".")

    raw, wrapped = build_wrapped_agent(bench, _NoVendorAgent, vendor="minimax")

    assert isinstance(raw, _NoVendorAgent)
    assert wrapped is raw


def test_example_common_require_scenarios_accepts_single_path_and_raises_actionable_error(tmp_path):
    import pytest

    from examples._common import require_scenarios

    with pytest.raises(FileNotFoundError) as exc_info:
        require_scenarios(tmp_path, "missing.yaml", prepare_hint="Run prepare first.")

    assert "Run prepare first." in str(exc_info.value)


def test_example_common_require_scenarios_reports_combined_missing_files(tmp_path):
    import pytest

    from examples._common import require_scenarios

    (tmp_path / "present.yaml").write_text("scenario: present\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError) as exc_info:
        require_scenarios(tmp_path, ["present.yaml", "missing-a.yaml", "missing-b.yaml"], prepare_hint="Run prepare.")

    message = str(exc_info.value)
    assert "missing-a.yaml" in message
    assert "missing-b.yaml" in message
    assert "present.yaml" not in message


def test_example_common_discovers_generated_scenarios(tmp_path):
    from examples._common import discover_generated_scenarios

    scenario_dir = tmp_path / "scenarios" / "generated" / "xs"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "b.yaml").write_text("scenario: b\n", encoding="utf-8")
    (scenario_dir / "a.yaml").write_text("scenario: a\n", encoding="utf-8")

    scenarios = discover_generated_scenarios(tmp_path, "xs")

    assert [path.name for path in scenarios] == ["a.yaml", "b.yaml"]


def test_example_common_help_mentions_pythonpath_and_api_key():
    from examples._common import build_arg_parser

    help_text = build_arg_parser("Example help").format_help()

    assert "PYTHONPATH=." in help_text
    assert "MINIMAX_API_KEY" in help_text
    assert "OPENAI_API_KEY" in help_text
    assert "--repo-root" in help_text
    assert "NetOpsBench checkout" in help_text


def _direct_script_smoke(driver: str, argv=None) -> None:
    repo = Path(__file__).resolve().parents[1]
    script = f"""
import runpy, sys, types
from pathlib import Path

sys.argv = [{driver!r}, *{list(argv or [])!r}]

class _FakeReport:
    def pretty_print(self):
        print('REPORT.pretty_print')

class _FakeRun:
    id = 'run-001'
    runtime_id = 'runtime-001'
    status = 'completed'
    def wait(self, raise_on_failure=False):
        return _FakeReport()

class _FakeRuntime:
    def __init__(self):
        self.name = 'runtime-001'
        self.state = 'ready'
        self.size = 2
        self.workers = []
    def teardown(self):
        self.state = 'deleted'

class _FakeRuntimes:
    def provision(self, **kwargs):
        return _FakeRuntime()

class _FakeSessions:
    def run_scenario(self, **kwargs):
        return _FakeRun()
    def run_suite(self, **kwargs):
        return _FakeRun()
    def run_on_runtime_scenario(self, **kwargs):
        return _FakeRun()
    def run_on_runtime_suite(self, **kwargs):
        return _FakeRun()

class _FakeFaultsInline:
    def register_pack(self, pack):
        pass


class _FakeAgentsInline:
    def wrap(self, agent):
        return agent

class NetOpsBench:
    def __init__(self, workspace):
        self.workspace = workspace
        self.runtimes = _FakeRuntimes()
        self.sessions = _FakeSessions()
        self.faults = _FakeFaultsInline()
        self.agents = _FakeAgentsInline()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

class MinimalDeepAgent:
    def __init__(self, *args, **kwargs):
        pass

    async def aclose(self):
        return None

fake_sdk = types.ModuleType('netopsbench.sdk')
fake_sdk.NetOpsBench = NetOpsBench
fake_sdk.RunFailedError = RuntimeError
sys.modules['netopsbench.sdk'] = fake_sdk

fake_examples_agents = types.ModuleType('examples.agents')
fake_examples_agents.MinimalDeepAgent = MinimalDeepAgent
sys.modules['examples.agents'] = fake_examples_agents

runpy.run_path({driver!r}, run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_01_run_scenario_can_run_as_direct_script():
    _direct_script_smoke("examples/01_run_scenario.py")


def test_02_run_suite_can_run_as_direct_script():
    _direct_script_smoke("examples/02_run_suite.py")


def test_05_manual_runtime_can_run_as_direct_script():
    _direct_script_smoke("examples/05_manual_runtime.py")


def test_03_run_scale_benchmark_can_run_as_direct_script():
    _direct_script_smoke("examples/03_run_scale_benchmark.py", ["--scale", "xs"])


def test_example_agent_passes_tools_skills_and_backend_to_deepagent(monkeypatch):
    import examples.agents.minimal_deepagent.agent as agent_mod

    captured = {}

    class _FakeChat:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeBackend:
        def __init__(self, *, root_dir, virtual_mode):
            self.root_dir = root_dir
            self.virtual_mode = virtual_mode

    class _FakeSession:
        async def initialize(self):
            pass

    class _FakeAsyncCtx:
        def __init__(self, val):
            self._val = val

        async def __aenter__(self):
            return self._val

        async def __aexit__(self, *a):
            return False

    def _fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _FakeGraph:
            async def ainvoke(self, payload, config=None):
                return {"messages": [SimpleNamespace(type="ai", content='```json\n{"verdict":"inconclusive"}\n```')]}

        return _FakeGraph()

    async def _fake_connect_mcp_tools(exit_stack, server_config):
        return [SimpleNamespace(name="get_topology")]

    monkeypatch.setattr(agent_mod, "_connect_mcp_tools", _fake_connect_mcp_tools)
    monkeypatch.setattr(agent_mod, "FilesystemBackend", _FakeBackend)
    monkeypatch.setattr(agent_mod, "create_deep_agent", _fake_create_deep_agent)

    agent = MinimalDeepAgent(
        api_key="test-key",
        mcp_server_config={"netopsbench": {"transport": "stdio"}},
    )
    context = DiagnosticContext(scenario_id="scenario-skills", topology={"devices": {}}, symptoms={})
    asyncio.run(agent.diagnose(context))

    assert "response_format" not in captured
    assert captured["skills"] == ["/skills/"]
    assert captured["backend"].virtual_mode is True
    assert [tool.name for tool in captured["tools"]] == ["get_topology"]


def test_minimal_deepagent_uses_sdk_default_mcp_server_config(monkeypatch):
    import examples.agents.minimal_deepagent.agent as agent_mod
    import examples.agents.minimal_deepagent.providers.runtime as provider_runtime

    captured_config = {}

    monkeypatch.setattr(
        agent_mod,
        "builtin_mcp_server_config",
        lambda workspace, **kwargs: {
            "netopsbench": {
                "transport": "stdio",
                "command": "python",
                "args": ["scripts/toolkit/run_fastmcp_server.py"],
                "cwd": str(workspace),
                "env": {"PYTHONPATH": str(workspace)},
            }
        },
    )

    async def _capturing_connect_mcp_tools(exit_stack, server_config):
        captured_config.update(server_config["netopsbench"])
        return []

    class _FakeGraph:
        async def ainvoke(self, payload, config=None):
            return {"messages": [_diagnosis_json_message()]}

    monkeypatch.setattr(agent_mod, "_connect_mcp_tools", _capturing_connect_mcp_tools)
    monkeypatch.setattr(provider_runtime, "_connect_mcp_tools", _capturing_connect_mcp_tools)
    monkeypatch.setattr(agent_mod, "FilesystemBackend", lambda **kw: None)
    monkeypatch.setattr(agent_mod, "create_deep_agent", lambda **kw: _FakeGraph())

    agent = MinimalDeepAgent(api_key="test-key")
    context = DiagnosticContext(scenario_id="scenario-mcp", topology={"devices": {}}, symptoms={})
    asyncio.run(agent.diagnose(context))

    assert captured_config["args"] == ["scripts/toolkit/run_fastmcp_server.py"]
