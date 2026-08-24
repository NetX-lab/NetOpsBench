import importlib
import os

from examples.agents.diagnostic_harness import DiagnosticHarness


class FakeRun:
    def wait(self, raise_on_failure=True):
        return self

    def pretty_print(self):
        return None


class FakeSessions:
    def __init__(self):
        self.calls = []

    def run_suite(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRun()


class FakeBench:
    last = None

    def __init__(self, workspace, scale_profiles=()):
        self.workspace = workspace
        self.scale_profiles = list(scale_profiles)
        self.sessions = FakeSessions()
        self.agents = type("Agents", (), {"wrap": staticmethod(lambda agent: agent)})()
        FakeBench.last = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeAgent:
    name = "fake"


def _prepare_repo(tmp_path):
    repo = tmp_path / "repo"
    root = repo / "scenarios" / "generated" / "medium"
    root.mkdir(parents=True)
    for index in range(28):
        (root / f"scenario-{index:02d}.yaml").write_text("schema_version: '1'\n", encoding="utf-8")
    return repo


def test_scale_benchmark_wraps_all_medium_scenarios_with_harness_and_profile(tmp_path):
    module = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_repo(tmp_path)
    profile = repo / "medium-isolated.yaml"
    profile.write_text("schema_version: '1'\n", encoding="utf-8")

    assert (
        module.main(
            repo_root=repo,
            scale="medium",
            workers=1,
            agent_mode="harness",
            scale_profile=profile,
            bench_cls=FakeBench,
            agent_cls=FakeAgent,
        )
        == 0
    )

    call = FakeBench.last.sessions.calls[0]
    assert len(call["scenarios"]) == 28
    assert call["workers"] == 1
    assert isinstance(call["agent"], DiagnosticHarness)
    assert FakeBench.last.scale_profiles == [profile]


def test_scale_benchmark_preserves_original_agent_rollback(tmp_path):
    module = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_repo(tmp_path)

    module.main(
        repo_root=repo,
        scale="medium",
        workers=1,
        agent_mode="original",
        bench_cls=FakeBench,
        agent_cls=FakeAgent,
    )

    assert isinstance(FakeBench.last.sessions.calls[0]["agent"], FakeAgent)


def test_scale_benchmark_env_does_not_override_exported_key(tmp_path, monkeypatch):
    module = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_repo(tmp_path)
    (repo / ".env").write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "already-exported")

    module.main(
        repo_root=repo,
        scale="medium",
        workers=1,
        bench_cls=FakeBench,
        agent_cls=FakeAgent,
    )

    assert os.environ["DEEPSEEK_API_KEY"] == "already-exported"


def test_scale_benchmark_allows_multiple_medium_harness_workers(tmp_path):
    module = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_repo(tmp_path)

    module.main(
        repo_root=repo,
        scale="medium",
        workers=4,
        agent_mode="harness",
        bench_cls=FakeBench,
        agent_cls=FakeAgent,
    )

    call = FakeBench.last.sessions.calls[0]
    assert call["workers"] == 4
    assert isinstance(call["agent"], DiagnosticHarness)


def test_scale_benchmark_can_select_exact_scenarios(tmp_path):
    module = importlib.import_module("examples.03_run_scale_benchmark")
    repo = _prepare_repo(tmp_path)
    selected = ("scenario-00", "scenario-27")

    module.main(
        repo_root=repo,
        scale="medium",
        workers=1,
        agent_mode="harness",
        only_scenario_ids=selected,
        bench_cls=FakeBench,
        agent_cls=FakeAgent,
    )

    call = FakeBench.last.sessions.calls[0]
    assert [path.stem for path in call["scenarios"]] == list(selected)
