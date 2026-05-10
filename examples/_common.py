"""Shared helpers for public example scripts.

These helpers intentionally live under ``examples/`` so they keep the example
scripts small without becoming part of the public SDK surface.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from netopsbench.sdk import RunFailedError

# Single source of truth for the LLM provider choices accepted by the bundled
# ``MinimalDeepAgent`` examples. Keep this list in sync with
# ``examples/agents/minimal_deepagent/providers``.
VENDOR_CHOICES: list[str] = ["minimax", "zhipu", "deepseek", "openai", "kimi"]
DEFAULT_VENDOR: str = "minimax"


def resolve_repo_root(repo_root: Path | str | None = None) -> Path:
    """Resolve the repository root used by example scripts."""
    return Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]


def require_scenarios(repo: Path, relative_paths: str | list[str], *, prepare_hint: str | None = None) -> list[Path]:
    """Return required scenario paths or raise one user-actionable error."""
    paths = [relative_paths] if isinstance(relative_paths, str) else list(relative_paths)
    scenarios = [repo / relative_path for relative_path in paths]
    missing = [str(path) for path in scenarios if not path.exists()]
    if not missing:
        return scenarios

    hint = prepare_hint or "Generate benchmark assets first with `netopsbench benchmark prepare --scales xs`."
    if len(scenarios) == 1:
        raise FileNotFoundError(f"Scenario not found: {scenarios[0]}. {hint}")
    raise FileNotFoundError(f"Scenario files missing: {missing}. {hint}")


def discover_generated_scenarios(repo: Path, scale: str) -> list[Path]:
    """Return sorted generated benchmark scenarios for one topology scale."""
    scenario_dir = repo / "scenarios" / "generated" / scale
    if not scenario_dir.is_dir():
        raise FileNotFoundError(
            f"No generated scenarios for scale '{scale}': {scenario_dir}. "
            f"Generate them first with `netopsbench benchmark prepare --scales {scale}`."
        )
    scenarios = sorted(scenario_dir.glob("*.yaml"))
    if not scenarios:
        raise FileNotFoundError(
            f"No .yaml files found in {scenario_dir}. "
            f"Generate them first with `netopsbench benchmark prepare --scales {scale}`."
        )
    return scenarios


def build_wrapped_agent(bench: Any, agent_cls: Any, *, vendor: str | None = None) -> tuple[Any, Any]:
    """Construct an example agent and wrap it when the SDK manager is available."""
    try:
        raw_agent = agent_cls(vendor=vendor) if vendor is not None else agent_cls()
    except TypeError:
        # Keep compatibility with tiny fake agents used by tests and docs.
        raw_agent = agent_cls()

    agents = getattr(bench, "agents", None)
    wrap = getattr(agents, "wrap", None)
    wrapped_agent = wrap(raw_agent) if callable(wrap) else raw_agent
    return raw_agent, wrapped_agent


def wait_and_print_run(run: Any, *, raise_on_failure: bool = True) -> int:
    """Wait for a run, print its report, and convert common failures to exit codes."""
    try:
        report = run.wait(raise_on_failure=raise_on_failure)
        report.pretty_print()
        return 0
    except RunFailedError as exc:
        print(f"Run failed: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — example script boundary
        print(f"Failed: {type(exc).__name__}: {exc}")
        return 1


def build_arg_parser(
    description: str,
    *,
    with_vendor: bool = True,
    with_repo_root: bool = True,
) -> argparse.ArgumentParser:
    """Return a pre-populated ``ArgumentParser`` shared by every example script."""
    parser = argparse.ArgumentParser(description=description)
    if with_vendor:
        parser.add_argument("--vendor", default=DEFAULT_VENDOR, choices=VENDOR_CHOICES)
    if with_repo_root:
        parser.add_argument("--repo-root", type=Path, default=None)
    return parser


def print_agent_banner(label: str, vendor: str, raw_agent: Any) -> None:
    """Print the standard ``vendor / model`` banner used by every example."""
    model = getattr(raw_agent, "model", "?")
    print(f"  {label}: vendor={vendor}  model={model}")
