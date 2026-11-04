"""Adapt native `codex exec` to the existing public diagnostic-agent contract."""

import asyncio
import json
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

from examples.agents.diagnosis import SYSTEM_GUIDANCE, DiagnosisOutput, build_result, build_user_prompt
from netopsbench.models.profiles import default_experiment_profile
from netopsbench.platform.scenario.timing import agent_cleanup_timing
from netopsbench.platform.toolkit._core.control import current_tool_control, diagnosis_budget_scope
from netopsbench.platform.toolkit.mcp.registry import tool_schemas
from netopsbench.platform.toolkit.mcp.session_server import session_mcp
from netopsbench.platform.utils.control import finish_cleanup
from netopsbench.sdk.agents import DiagnosisResult

from .proxy import openai_proxy
from .runtime import CLI_VERSION, DEFAULT_IMAGE, DiagnosticContainer, credentials, ensure_image, image_content_identity


def strict_output_schema():
    schema = DiagnosisOutput.model_json_schema()

    def strict(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            node.pop("default", None)
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(schema)
    return schema


def redact(value, secrets):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED]", text)
    return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", text)


class CodexEvents:
    def __init__(self, trace=None):
        self.trace = trace
        self.items = {}
        self.usage = {}
        self.final = None
        self.error = None
        self.completed = False

    def accept(self, event):
        kind = event.get("type")
        if kind == "turn.completed":
            self.completed = True
            usage = event.get("usage") or {}
            self.usage = {
                key: int(usage.get(key, 0) or 0) for key in ("input_tokens", "output_tokens", "cached_input_tokens")
            }
            self.usage["total_tokens"] = self.usage["input_tokens"] + self.usage["output_tokens"]
        if kind in {"turn.failed", "error"}:
            self.error = str(event.get("error") or event.get("message") or "Codex turn failed")
        item = event.get("item") or {}
        if item.get("id"):
            self.items[item["id"]] = item
        if kind == "item.completed" and item.get("type") == "agent_message":
            self.final = item.get("text")
        # MCP calls are traced by the gateway; only shell events are recorded here.
        if item.get("type") == "command_execution" and self.trace:
            run_id = f"codex-{item['id']}"
            if kind == "item.started":
                self.trace.record_tool_start(name="shell", args={"command": item.get("command")}, run_id=run_id)
            elif kind == "item.completed":
                self.trace.record_tool_end(
                    output={"output": item.get("aggregated_output"), "exit_code": item.get("exit_code")}, run_id=run_id
                )

    def result(self, exit_code):
        if exit_code != 0 or self.error or not self.completed:
            raise RuntimeError(self.error or f"Codex did not complete (exit={exit_code})")
        if not self.final:
            raise ValueError("Codex returned no structured diagnosis")
        return DiagnosisOutput.model_validate_json(self.final).model_dump()

    def counts(self):
        return {
            "mcp_calls": sum(item.get("type") == "mcp_tool_call" for item in self.items.values()),
            "shell_calls": sum(item.get("type") == "command_execution" for item in self.items.values()),
            "failed_mcp_calls": sum(
                item.get("type") == "mcp_tool_call" and item.get("status") == "failed" for item in self.items.values()
            ),
        }


class CodexAgent:
    def __init__(
        self,
        *,
        name="codex",
        model="gpt-6.1-sol",
        reasoning_effort="xhigh",
        cli_version=CLI_VERSION,
        image=DEFAULT_IMAGE,
        timeout_seconds=None,
        max_tool_calls=40,
        auth_store=None,
        artifacts_dir=None,
    ):
        self.name, self.model, self.reasoning_effort = name, model, reasoning_effort
        self.service_tier = "default"
        self.cli_version, self.image = cli_version, image
        self.timeout_seconds = (
            default_experiment_profile().diagnosis_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self.max_tool_calls = max_tool_calls
        self.auth_store = Path(auth_store or ".netopsbench/private/codex-auth").resolve()
        self._refresh_from_host = auth_store is None
        self.artifacts_dir = Path(artifacts_dir or ".netopsbench/codex-traces").resolve()
        self._containers = set()
        self._image_id = None
        self._image_content_sha256 = None

    def preflight(self):
        credentials(self.auth_store, refresh_from_host=self._refresh_from_host)
        self._image_id = ensure_image(image=self.image, version=self.cli_version)
        self._image_content_sha256 = image_content_identity(self._image_id)
        return {
            "cli_version": self.cli_version,
            "image_id": self._image_id,
            "image_content_sha256": self._image_content_sha256,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "timeout_seconds": self.timeout_seconds,
            "max_mcp_calls": self.max_tool_calls,
        }

    def cli_args(self, endpoint):
        args = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "--cd",
            "/workspace",
            "--model",
            self.model,
            "--output-schema",
            "/input/schema.json",
        ]
        config = {
            "model_reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "web_search": "disabled",
            "developer_instructions": SYSTEM_GUIDANCE,
            "mcp_servers.netopsbench.url": endpoint["url"],
            "mcp_servers.netopsbench.bearer_token_env_var": "DIAGNOSTIC_MCP_TOKEN",
            "mcp_servers.netopsbench.required": True,
            "mcp_servers.netopsbench.enabled_tools": [tool["name"] for tool in tool_schemas()],
        }
        for feature in (
            "fast_mode",
            "ultrafast_mode",
            "apps",
            "plugins",
            "remote_plugin",
            "multi_agent",
            "browser_use",
            "browser_use_external",
            "computer_use",
            "skill_search",
        ):
            config[f"features.{feature}"] = False
        for key, value in config.items():
            args.extend(["-c", f"{key}={json.dumps(value)}"])
        skills = ["imagegen", "openai-docs", "review-agent", "skill-creator", "skill-installer"]
        overrides = ",".join(
            "{path=" + json.dumps(f"/home/diagnostic/.codex/skills/.system/{name}/SKILL.md") + ",enabled=false}"
            for name in skills
        )
        args.extend(["-c", f"skills.config=[{overrides}]"])
        return [*args, "-"]

    async def diagnose(self, context):
        try:
            async with diagnosis_budget_scope(self.timeout_seconds):
                return await self._execute(context, build_user_prompt(context, fenced_output=False))
        except TimeoutError as exc:
            return DiagnosisResult(
                agent_name=self.name,
                verdict="inconclusive",
                success=False,
                reasoning="Total diagnosis timeout exceeded",
                metadata={
                    **getattr(exc.__cause__, "agent_metadata", {}),
                    "error_type": "TimeoutError",
                    "agent_failure_stage": "diagnose",
                    "timeout_seconds": self.timeout_seconds,
                },
            )

    async def check_connection(self):
        from types import SimpleNamespace

        from netopsbench.platform.toolkit._core.common import ToolResult
        from netopsbench.sdk.agents import DiagnosticContext

        context = DiagnosticContext(
            scenario_id="preflight",
            topology={},
            symptoms={},
            tools=SimpleNamespace(get_topology=lambda **kwargs: ToolResult(True, {"preflight": "ok"})),
        )
        result = await self._execute(
            context,
            (
                "This is a connectivity preflight, not a network diagnosis. Call get_topology once, "
                "run python3 -c 'print(2+2)' in the shell, then return JSON with verdict network_healthy, "
                "fault_type null, location {device:null,interface:null}, evidence [preflight ok], "
                "confidence 1 and reasoning preflight complete. Do not call other tools."
            ),
        )
        if (
            not result.success
            or result.verdict != "network_healthy"
            or result.metadata.get("failed_mcp_calls")
            or result.metadata.get("mcp_calls", 0) < 1
            or result.metadata.get("shell_calls", 0) < 1
        ):
            raise RuntimeError(f"Codex connectivity preflight failed: {result.reasoning}")
        return result.metadata

    async def _execute(self, context, prompt):
        metadata = {
            "runtime": "codex-cli+isolated-shell+mcp",
            "provider": "codex",
            "model": self.model,
            "cli_version": self.cli_version,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "max_mcp_calls": self.max_tool_calls,
            "timeout_seconds": self.timeout_seconds,
        }
        events = CodexEvents(context.trace)
        container = DiagnosticContainer(self.image, self.auth_store)
        self._containers.add(container)
        directory = self.artifacts_dir / uuid.uuid4().hex
        directory.mkdir(parents=True)
        metadata["codex_events_path"] = str(directory / "events.jsonl")
        metadata["codex_stderr_path"] = str(directory / "stderr.txt")
        process = None
        auth_temporary = None
        open_task = None
        secrets = []
        result = None
        try:
            if self._image_id is None:
                await asyncio.to_thread(self.preflight)
            metadata["image_id"] = self._image_id
            metadata["image_content_sha256"] = self._image_content_sha256
            container.image = self._image_id
            secrets = credentials(self.auth_store, refresh_from_host=self._refresh_from_host)
            auth_temporary = tempfile.TemporaryDirectory(prefix="netopsbench-codex-auth-")
            container.auth_store = Path(auth_temporary.name)
            shutil.copyfile(self.auth_store / "auth.json", container.auth_store / "auth.json")
            (container.auth_store / "auth.json").chmod(0o600)
            open_task = asyncio.create_task(asyncio.to_thread(container.open))
            await asyncio.shield(open_task)
            with tempfile.TemporaryDirectory(prefix="netopsbench-codex-input-") as temporary:
                inputs = Path(temporary)
                inputs.chmod(0o755)
                (inputs / "schema.json").write_text(json.dumps(strict_output_schema()))
                (inputs / "observation.json").write_text(
                    json.dumps(
                        {
                            "case_id": context.scenario_id,
                            "topology": context.topology,
                            "symptoms": context.symptoms,
                        },
                        default=str,
                    )
                )
                with openai_proxy(container.gateway) as proxy_port:
                    async with session_mcp(
                        context, host=container.gateway, max_tool_calls=self.max_tool_calls
                    ) as endpoint:
                        secrets.append(endpoint["token"])
                        command = container.command(
                            inputs,
                            mcp_port=endpoint["port"],
                            proxy_port=proxy_port,
                            token=endpoint["token"],
                            cli_args=self.cli_args(endpoint),
                        )
                        control = current_tool_control()
                        if control is not None:
                            control.remaining()
                        process = await asyncio.create_subprocess_exec(
                            *command,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            limit=8 * 1024 * 1024,
                        )
                        process.stdin.write(prompt.encode())
                        await process.stdin.drain()
                        process.stdin.close()

                        async def collect_stdout():
                            with (directory / "events.jsonl").open("w") as output:
                                while line := await process.stdout.readline():
                                    cleaned = redact(line.decode(errors="replace"), secrets)
                                    output.write(cleaned)
                                    output.flush()
                                    events.accept(json.loads(cleaned))

                        async def collect_stderr():
                            with (directory / "stderr.txt").open("w") as output:
                                while line := await process.stderr.readline():
                                    output.write(redact(line.decode(errors="replace"), secrets))

                        cli_timeout = (
                            control.remaining(self.timeout_seconds) if control is not None else self.timeout_seconds
                        )
                        await asyncio.wait_for(
                            asyncio.gather(collect_stdout(), collect_stderr(), process.wait()),
                            timeout=cli_timeout,
                        )
                        metadata.update(events.usage, **events.counts(), exit_code=process.returncode)
                        if metadata["mcp_calls"] > self.max_tool_calls:
                            raise RuntimeError("MCP tool-call budget exhausted")
                        output = events.result(process.returncode)
                        metadata["tool_calls"] = context.trace.tool_calls() if context.trace else []
                        result = build_result(self.name, output, metadata=metadata)
        except asyncio.CancelledError as exc:
            metadata.update(events.usage, **events.counts(), agent_failure_stage="diagnose")
            exc.agent_metadata = metadata
            raise
        except Exception as exc:
            metadata.update(
                events.usage, **events.counts(), error_type=type(exc).__name__, agent_failure_stage="diagnose"
            )
            error = redact(str(exc), secrets)
            stderr = directory / "stderr.txt"
            if stderr.exists() and stderr.stat().st_size:
                error += ": " + stderr.read_text()[-2000:]
            result = DiagnosisResult(
                agent_name=self.name, verdict="inconclusive", success=False, reasoning=error, metadata=metadata
            )
        finally:
            with agent_cleanup_timing():
                cleanup_started = time.monotonic()
                cleanup_result, cancelled = await finish_cleanup(
                    self._cleanup(container, process, auth_temporary, open_task)
                )
                errors = redact(cleanup_result, secrets)
                metadata["agent_cleanup_seconds"] = time.monotonic() - cleanup_started
                metadata["cleanup_errors"] = json.loads(errors)
                if container in self._containers:
                    metadata["retained_resources"] = {
                        "container": getattr(container, "name", None),
                        "network": getattr(container, "network", None),
                    }
                if metadata["cleanup_errors"] and result is not None:
                    result = replace(
                        result, success=False, reasoning=result.reasoning + "; diagnostic resource cleanup failed"
                    )
                if result is not None:
                    result.metadata.update(metadata)
                (directory / "cleanup.json").write_text(
                    json.dumps(
                        {
                            "errors": metadata["cleanup_errors"],
                            "retained_resources": metadata.get("retained_resources", {}),
                        },
                        indent=2,
                    )
                )
                if cancelled:
                    raise asyncio.CancelledError
        return result

    async def _cleanup(self, container, process, auth_temporary, open_task):
        errors = []
        if open_task is not None:
            try:
                await open_task
            except Exception as exc:
                errors.append(f"startup: {exc}")
        if process is not None and process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            except (OSError, ProcessLookupError) as exc:
                errors.append(f"process: {exc}")
        try:
            await asyncio.to_thread(container.close)
        except Exception as exc:
            errors.append(str(exc))
        else:
            self._containers.discard(container)
        if auth_temporary is not None:
            try:
                refreshed = container.auth_store / "auth.json"
                if refreshed.exists():
                    from netopsbench.platform.utils.files import atomic_write_json

                    auth = json.loads(refreshed.read_text())
                    if not isinstance(auth, dict) or not (auth.get("tokens") or auth.get("OPENAI_API_KEY")):
                        raise ValueError("Invalid refreshed Codex authentication")
                    atomic_write_json(self.auth_store / "auth.json", auth)
                    (self.auth_store / "auth.json").chmod(0o600)
            except (ValueError, OSError) as exc:
                errors.append(f"credential refresh: {exc}")
            finally:
                try:
                    auth_temporary.cleanup()
                except OSError as exc:
                    errors.append(f"temporary credentials: {exc}")
        return errors

    async def aclose(self):
        errors = []
        for container in list(self._containers):
            try:
                await asyncio.to_thread(container.close)
            except Exception as exc:
                errors.append(str(exc))
            else:
                self._containers.discard(container)
        if errors:
            raise RuntimeError("; ".join(errors))
