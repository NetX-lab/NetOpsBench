"""Static dependency rules for the modular NetOpsBench implementation."""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "netopsbench"


def _python_files(root: Path = PACKAGE_ROOT) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _module_name(path: Path) -> str:
    module = ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
    return module.removesuffix(".__init__")


def _imports(path: Path) -> set[str]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = package.split(".")
                prefix = package_parts[: len(package_parts) - node.level + 1]
                base = ".".join(prefix + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base:
                imported.add(base)
            if not node.module:
                imported.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return imported


def test_platform_does_not_depend_on_public_sdk():
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            name for name in _imports(path) if name.startswith("netopsbench.sdk")
        )
        for path in _python_files(PACKAGE_ROOT / "platform")
    }
    assert not {path: imports for path, imports in offenders.items() if imports}


def test_internal_module_graph_has_no_import_cycles():
    files_by_module = {_module_name(path): path for path in _python_files()}
    graph = {
        module: {target for target in _imports(path) if target in files_by_module and target != module}
        for module, path in files_by_module.items()
    }
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> list[str] | None:
        if module in visiting:
            start = visiting.index(module)
            return visiting[start:] + [module]
        if module in visited:
            return None
        visiting.append(module)
        for dependency in graph[module]:
            if cycle := visit(dependency):
                return cycle
        visiting.pop()
        visited.add(module)
        return None

    for module in graph:
        cycle = visit(module)
        assert cycle is None, " -> ".join(cycle or [])


def test_domain_and_service_modules_do_not_read_process_environment():
    roots = [
        PACKAGE_ROOT / "models",
        PACKAGE_ROOT / "platform" / "faults" / "handlers",
        PACKAGE_ROOT / "platform" / "faults" / "services",
    ]
    files = [path for root in roots for path in _python_files(root)]
    files.extend(
        PROJECT_ROOT / relative
        for relative in (
            "netopsbench/platform/faults/models.py",
            "netopsbench/platform/scenario/generator.py",
            "netopsbench/platform/scenario/validator.py",
            "netopsbench/platform/topology/clos_builder.py",
            "netopsbench/platform/topology/fat_tree_builder.py",
            "netopsbench/platform/topology/plan.py",
            "netopsbench/platform/topology/renderer.py",
            "netopsbench/platform/traffic/generator.py",
            "netopsbench/platform/traffic/planner.py",
            "netopsbench/platform/session/diagnosis.py",
            "netopsbench/platform/session/reporting.py",
        )
    )
    forbidden = ("os.environ", "os.getenv", "netopsbench.config")
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): [token for token in forbidden if token in path.read_text(encoding="utf-8")]
        for path in files
    }
    assert not {path: tokens for path, tokens in offenders.items() if tokens}


def test_removed_environment_controls_do_not_return():
    removed = {
        "NETOPSBENCH_ACTIVE_INTERFACE_COVERAGE_MIN_RATIO",
        "NETOPSBENCH_AGENT_TIMEOUT_SECONDS",
        "NETOPSBENCH_APPLY_CONFIG_PARALLEL",
        "NETOPSBENCH_BGP_COLLECTOR_MAX_BYTES",
        "NETOPSBENCH_BGP_COLLECTOR_PARALLELISM",
        "NETOPSBENCH_BGP_POLL_INTERVAL_SECONDS",
        "NETOPSBENCH_CONTAINERLAB_MAX_WORKERS",
        "NETOPSBENCH_CONTAINERLAB_TIMEOUT",
        "NETOPSBENCH_GRAFANA_DEFAULT_BUCKET",
        "NETOPSBENCH_GRAFANA_INFLUXDB_URL",
        "NETOPSBENCH_GRAFANA_PASSWORD",
        "NETOPSBENCH_GRAFANA_URL",
        "NETOPSBENCH_PINGMESH_DEPLOY_PARALLELISM",
        "NETOPSBENCH_PINGMESH_INFLUXDB_URL",
        "NETOPSBENCH_RUNTIME_ID",
        "NETOPSBENCH_SFLOW_",
        "NETOPSBENCH_SONIC_LINK_WAIT_INTERVAL",
        "NETOPSBENCH_SONIC_LINK_WAIT_TIMEOUT",
        "NETOPSBENCH_SONIC_WAIT_TRIES",
        "NETOPSBENCH_SWITCH_PPS_LIMIT",
        "NETOPSBENCH_SYSLOG_COLLECTOR",
        "NETOPSBENCH_TELEGRAF_INFLUXDB_URL",
        "NETOPSBENCH_TRACE",
        "NETOPSBENCH_WORKER_AGENT_TIMEOUT_SECONDS",
        "NETOPSBENCH_WORKER_DEPLOY_JOBS",
        "NETOPSBENCH_WORKER_DEPLOY_TIMEOUT",
        "NETOPSBENCH_WORKER_DISABLE_LANGSMITH",
        "NETOPSBENCH_WORKER_HEALTH_DELAY_SECONDS",
        "NETOPSBENCH_WORKER_HEALTH_RETRIES",
        "NETOPSBENCH_WORKER_ID",
        "PINGMESH_CYCLE_INTERVAL",
        "SONIC_GNMI_",
    }
    roots = [
        PACKAGE_ROOT,
        PROJECT_ROOT / "docs",
        PROJECT_ROOT / "examples",
        PROJECT_ROOT / "scenarios",
        PROJECT_ROOT / "scripts",
    ]
    files = [path for root in roots for path in root.rglob("*") if path.is_file()]
    files.append(PROJECT_ROOT / ".env.example")
    offenders: dict[str, list[str]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matches = sorted(token for token in removed if token in text)
        if matches:
            offenders[str(path.relative_to(PROJECT_ROOT))] = matches
    assert not offenders


def test_library_modules_do_not_call_sys_exit():
    cli_boundaries = {
        PACKAGE_ROOT / "cli" / "main.py",
    }
    offenders: list[str] = []
    for path in _python_files():
        if path in cli_boundaries:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
                and node.func.attr == "exit"
            ):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert not offenders


def test_source_uses_timezone_aware_utc_clock():
    forbidden = ("datetime.utcnow(", "datetime.now()")
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): [token for token in forbidden if token in path.read_text(encoding="utf-8")]
        for path in _python_files()
    }
    offenders = {path: tokens for path, tokens in offenders.items() if tokens}
    assert not offenders


def test_runtime_lifecycle_cli_imports_without_package_cycle():
    result = subprocess.run(
        [sys.executable, "-m", "netopsbench.platform.runtime.cli", "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_client_image_docker_context_excludes_repository_artifacts():
    patterns = set((PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {".git", ".netopsbench*", "scenario_results", "native/client-agent/target"} <= patterns
    assert "native/client-agent" not in patterns
    assert "containers" not in patterns


def test_removed_internal_compatibility_trees_do_not_return():
    removed = [
        PACKAGE_ROOT / "resources",
        PACKAGE_ROOT / "screenshots",
        PACKAGE_ROOT / "platform" / "worker",
        PROJECT_ROOT / "observability",
        PROJECT_ROOT / "scripts" / "observability",
    ]
    assert not [path for path in removed if path.exists()]


def test_observability_does_not_depend_on_runtime_or_toolkit():
    forbidden_prefixes = (
        "netopsbench.platform.runtime",
        "netopsbench.platform.toolkit",
    )
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden_prefixes)
        )
        for path in _python_files(PACKAGE_ROOT / "platform" / "observability")
    }
    assert not {path: imports for path, imports in offenders.items() if imports}


def test_removed_platform_state_and_features_do_not_return():
    forbidden = (
        "TopologyState",
        "TopologySpec",
        "WorkerSpec",
        "_discover_topology",
        "NETOPSBENCH_ALLOW_TOPOLOGY_FALLBACK",
        "sflow",
        "screenshot",
    )
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): [
            token for token in forbidden if token.lower() in path.read_text(encoding="utf-8").lower()
        ]
        for path in _python_files(PACKAGE_ROOT / "platform")
    }
    assert not {path: tokens for path, tokens in offenders.items() if tokens}


def test_platform_main_functions_match_declared_console_scripts():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared_modules = {
        target.split(":", 1)[0]
        for target in project["project"]["scripts"].values()
        if target.startswith("netopsbench.platform.")
    }
    entry_modules = {
        _module_name(path)
        for path in _python_files(PACKAGE_ROOT / "platform")
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
            for node in ast.parse(path.read_text(encoding="utf-8")).body
        )
    }
    assert entry_modules == declared_modules


def test_runtime_code_does_not_infer_a_source_checkout():
    forbidden = ("NETOPSBENCH_REPO_ROOT", "runtime_asset_root", "materialize_resource")
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): [token for token in forbidden if token in path.read_text(encoding="utf-8")]
        for path in _python_files()
    }
    assert not {path: tokens for path, tokens in offenders.items() if tokens}


def test_packaged_asset_trees_contain_only_runtime_inputs():
    observability_root = PACKAGE_ROOT / "platform" / "observability" / "assets"
    observability_files = {
        str(path.relative_to(observability_root)) for path in observability_root.rglob("*") if path.is_file()
    }
    assert observability_files == {
        "docker-compose.yaml",
        "telegraf.conf.template",
        "grafana/dashboards/network_overview.json",
        "grafana/dashboards/pingmesh.json",
        "grafana/provisioning/dashboards/default.yaml",
        "grafana/provisioning/datasources/default.yaml",
    }

    scenario_specs = PACKAGE_ROOT / "platform" / "scenario" / "specs"
    assert {path.name for path in scenario_specs.iterdir() if path.is_file()} == {"fault_campaign.yaml"}


def test_canonical_scenario_and_evaluator_are_single_source_of_truth(tmp_path):
    from netopsbench.agents.base import DiagnosticContext
    from netopsbench.models import profiles
    from netopsbench.models.scenario import ScenarioSpec
    from netopsbench.sdk import NetOpsBench

    assert set(ScenarioSpec.model_fields) >= {"scenario_id", "topology_scale", "episode"}
    assert "episodes" not in ScenarioSpec.model_fields
    assert "ground_truth" not in {field.name for field in dataclasses.fields(DiagnosticContext)}
    assert profiles.SCALE_PROFILES["xs"] is profiles.default_scale_registry().get("xs")
    assert not (PACKAGE_ROOT / "platform" / "scenario" / "models.py").exists()
    assert (PACKAGE_ROOT / "sdk" / "evaluators.py").exists()
    with NetOpsBench(workspace=tmp_path) as bench:
        assert hasattr(bench, "evaluators")


def test_core_distribution_contains_no_rl_training_control_plane():
    forbidden_imports = ("verl", "ray", "netopsbench_rl", "netopsbench.integrations")
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden_imports)
        )
        for path in _python_files()
    }
    assert not {path: imports for path, imports in offenders.items() if imports}

    forbidden_tokens = ("RLExperimentConfig", "TaskSampler", "train_split", "group_id", "group_size")
    source_offenders = {
        str(path.relative_to(PROJECT_ROOT)): [
            token for token in forbidden_tokens if token in path.read_text(encoding="utf-8")
        ]
        for path in _python_files()
    }
    assert not {path: tokens for path, tokens in source_offenders.items() if tokens}


def test_simulator_has_no_builtin_scale_name_branches():
    from netopsbench.models.profiles import default_scale_registry

    scale_names = set(default_scale_registry().names())
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT / "platform" / "simulator"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.Match)):
                continue
            values = {
                child.value
                for child in ast.walk(node.test if isinstance(node, ast.If) else node.subject)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            if values & scale_names:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert not offenders
