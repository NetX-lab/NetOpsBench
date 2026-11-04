#!/usr/bin/env python3
"""Sequential real-runtime acceptance; defaults to DeepAgent with DeepSeek."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values

from examples._common import agent_class
from netopsbench.platform.faults.catalog import FAULT_MECHANISMS
from netopsbench.platform.runtime.toolchain import runtime_tools
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.session.acceptance import run_acceptance
from netopsbench.platform.topology.config import DEFAULT_CLIENT_IMAGE, DEFAULT_SONIC_VS_IMAGE
from netopsbench.sdk import NetOpsBench


def select_cases(paths: list[Path], include_faults: bool, manifest) -> list[Path]:
    """The catalog already contains one case per necessary coverage unit."""
    cases = [parse_scenario_file(path) for path in paths]
    if not any(case.episode.is_healthy for case in cases):
        raise RuntimeError("Acceptance requires a healthy case")
    if any(case.metadata.get("fault_catalog_version") != 2 for case in cases):
        raise ValueError("Obsolete scenarios; regenerate fault catalog v2")
    healthy = sorted(path for path, case in zip(paths, cases, strict=True) if case.episode.is_healthy)
    return (
        healthy + sorted(path for path, case in zip(paths, cases, strict=True) if not case.episode.is_healthy)
        if include_faults
        else healthy
    )


def smoke_cases(paths, manifest):
    return select_cases(paths, True, manifest)


def preflight_agents(backends, vendor, output, *, expected=None):
    """Validate every configuration before making small real model requests."""
    expected = expected or {}
    identities, agents = {}, {}
    runtime_tools()
    image = os.environ.get("NETOPSBENCH_CLIENT_IMAGE", DEFAULT_CLIENT_IMAGE)
    subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True)
    subprocess.check_output(["docker", "image", "inspect", DEFAULT_SONIC_VS_IMAGE, "--format", "{{.Id}}"], text=True)
    for backend in backends:
        if backend == "deepagent":
            agent = agent_class(backend)(vendor=vendor)
            env = agent._provider.PRESET["api_key_env"]
            key = os.environ.get(env) or dotenv_values(Path.cwd() / ".env").get(env)
            if not key:
                raise RuntimeError(f"{env} must be configured before deployment")
            os.environ[env] = key
            agent.api_key = key
            identities[backend] = {
                "vendor": vendor,
                "model": agent.model,
                "base_url": agent.base_url,
                "max_graph_tool_steps": agent.max_tool_calls,
                "request_timeout_seconds": agent.timeout_seconds,
            }
        else:
            from examples.agents.codex_agent.runtime import image_content_identity

            agent = agent_class(backend)(artifacts_dir=output / "codex-preflight")
            if backend in expected:
                current = {
                    key: getattr(agent, key)
                    for key in ("cli_version", "model", "reasoning_effort", "service_tier", "timeout_seconds")
                }
                current["max_mcp_calls"] = agent.max_tool_calls
                current["image_id"] = subprocess.check_output(
                    ["docker", "image", "inspect", agent.image, "--format", "{{.Id}}"], text=True
                ).strip()
                current["image_content_sha256"] = image_content_identity(agent.image)
                if current != expected[backend]:
                    raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
                identities[backend] = current
        agents[backend] = agent
        if backend in expected and expected[backend] != identities[backend]:
            raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
    for backend, agent in agents.items():
        if backend == "deepagent":
            llm = agent._provider.build_llm(
                model=agent.model,
                api_key=agent.api_key,
                base_url=agent.base_url,
                temperature=agent.temperature,
                max_tokens=32,
                timeout_seconds=agent.timeout_seconds,
            )
            llm.invoke("Reply with OK only.")
        else:
            # Every resumed configuration was checked before auth writes or image execution.
            identities[backend] = agent.preflight()
            if backend in expected and identities[backend] != expected[backend]:
                raise ValueError(f"Cannot change {backend} configuration when resuming acceptance")
            asyncio.run(agent.check_connection())
    return identities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", default="medium,large,fat-tree-k8,fat-tree-k12,xlarge,fat-tree-k18")
    parser.add_argument("--mode", choices=["effects", "diagnose"], default="diagnose")
    parser.add_argument("--fault-types", nargs="+", choices=["none", *FAULT_MECHANISMS])
    parser.add_argument("--vendor", default="deepseek")
    parser.add_argument("--agents", nargs="+", choices=["deepagent", "codex"], default=["deepagent"])
    parser.add_argument("--output", type=Path, default=Path(".netopsbench/validation-pingmesh-v2"))
    parser.add_argument("--capacity-sample", type=Path)
    parser.add_argument("--allow-tight-capacity", action="store_true")
    parser.add_argument("--resume-runtime", type=Path)
    parser.add_argument("--rebuild-runtime", action="store_true")
    args = parser.parse_args()
    if args.fault_types and args.mode != "diagnose":
        parser.error("--fault-types selects diagnostic cases; effects requires the complete coverage set")

    def requested_cases(paths, include_faults, manifest):
        selected = select_cases(paths, include_faults, manifest)
        if args.fault_types:
            selected = [p for p in selected if parse_scenario_file(p).episode.fault_type in args.fault_types]
            if not selected:
                raise ValueError("No requested diagnostic types in this profile")
        return selected

    return run_acceptance(
        args,
        bench_factory=NetOpsBench,
        agent_factory=agent_class,
        preflight=preflight_agents,
        select_cases=requested_cases,
        smoke_cases=lambda paths, manifest: requested_cases(paths, True, manifest),
    )


if __name__ == "__main__":
    raise SystemExit(main())
