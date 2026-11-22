"""Run native Antigravity diagnostics against a single episode's MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.agents.diagnosis import SYSTEM_GUIDANCE, DiagnosisOutput, build_result, build_user_prompt
from netopsbench.agents.tracing import AgentTraceRecorder
from netopsbench.platform.toolkit._core.control import current_tool_control
from netopsbench.platform.toolkit.fastmcp_server import EXPOSED_TOOLS
from netopsbench.platform.toolkit.mcp.session_server import session_mcp
from netopsbench.sdk.agents import DiagnosisResult, DiagnosticContext


class AntigravityAgent:
    name = "antigravity"
    provider = "google"

    def __init__(self, *, binary=None, model="gemini-3.8-flash-medium", timeout_seconds=600, max_tool_calls=40):
        self.binary = Path(binary or shutil.which("agy") or Path.home() / ".local/bin/agy").resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tool_calls = max_tool_calls

    async def diagnose(self, context: DiagnosticContext) -> DiagnosisResult:
        if not context.trace or not context.trace.enabled:
            context = replace(context, trace=AgentTraceRecorder())
        metadata = {"runtime": "antigravity-cli", "provider": self.provider, "model": self.model}
        token = ""
        try:
            if not self.binary.is_file() or not shutil.which("bwrap"):
                raise RuntimeError("Install agy and bubblewrap before running Antigravity diagnostics")
            control = current_tool_control()
            timeout = control.remaining(self.timeout_seconds) if control else self.timeout_seconds
            async with session_mcp(context, max_tool_calls=self.max_tool_calls) as endpoint:
                token = endpoint["token"]
                envelope = await self._run(context, endpoint, timeout)
            for field in ("input_tokens", "output_tokens", "total_tokens", "thinking_tokens", "cache_read_tokens"):
                if field in envelope.get("usage", {}):
                    metadata[field] = envelope["usage"][field]
            if not context.trace.tool_calls():
                raise RuntimeError("Antigravity returned a verdict without querying episode MCP")
            return build_result(self.name, envelope["structured_output"], metadata=metadata)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            error = "Antigravity diagnosis timed out" if isinstance(exc, TimeoutError) else str(exc)
            if token:
                error = error.replace(token, "[redacted]")
            return DiagnosisResult(self.name, "inconclusive", success=False, reasoning=error, metadata=metadata)
        finally:
            metadata["tool_calls"] = context.trace.tool_calls()

    async def _run(self, context, endpoint, timeout):
        home = Path.home()
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "lang"}
        }
        env.update(HOME=str(home), PATH="/usr/bin:/bin")
        bypass = ",".join(filter(None, [env.get("NO_PROXY") or env.get("no_proxy"), "localhost,127.0.0.1,::1"]))
        env["NO_PROXY"] = env["no_proxy"] = bypass
        settings = {
            "permissions": {
                "allow": ["mcp(netopsbench/*)"],
                "deny": ["command(*)", "write_file(*)", "read_file(*)", "execute_url(*)", "read_url(*)"],
            }
        }
        with TemporaryDirectory(prefix="netopsbench-agy-") as temporary:
            directory = Path(temporary)
            private_home = directory / "home"
            workspace = directory / "workspace"
            (workspace / ".agents").mkdir(parents=True)
            state = private_home / ".gemini" / "antigravity-cli"
            state.mkdir(parents=True)
            if api_key := os.environ.get("GEMINI_API_KEY"):
                env["GEMINI_API_KEY"] = api_key
                settings["modelProvider"] = "gemini"
            else:
                auth = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                if not auth.is_file():
                    raise RuntimeError("No Antigravity credential found. Configure GEMINI_API_KEY or sign in with agy")
                shutil.copyfile(auth, state / auth.name)
                (state / auth.name).chmod(0o600)
            config = {
                "mcpServers": {
                    "netopsbench": {
                        "serverUrl": endpoint["url"],
                        "headers": {"Authorization": f"Bearer {endpoint['token']}"},
                        "tools": {name: {"eager": True} for name in EXPOSED_TOOLS},
                    }
                }
            }
            (workspace / ".agents" / "mcp_config.json").write_text(json.dumps(config))
            (workspace / "schema.json").write_text(json.dumps(DiagnosisOutput.model_json_schema()))
            prompt = SYSTEM_GUIDANCE + "\n\n" + build_user_prompt(context, fenced_output=False)
            prompt += "\nUse the finish tool to submit the structured diagnosis."
            command = ["bwrap", "--die-with-parent", "--unshare-pid"]
            for path in (
                "/usr",
                "/bin",
                "/lib",
                "/lib64",
                "/etc/ssl",
                "/etc/hosts",
                "/etc/resolv.conf",
                "/etc/nsswitch.conf",
            ):
                if Path(path).exists():
                    command += ["--ro-bind", path, path]
            command += [
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(private_home),
                str(home),
                "--ro-bind",
                str(workspace),
                "/work",
                "--ro-bind",
                str(self.binary),
                "/agy",
                "--chdir",
                "/work",
                "/agy",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--json-schema",
                "/work/schema.json",
                "--disable-slash-commands",
                "--settings",
                json.dumps(settings),
            ]
            if self.model:
                command += ["--model", self.model]
            process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            if not stdout:
                raise RuntimeError(f"Antigravity returned no JSON output (exit {process.returncode})")
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict):
                raise RuntimeError("Antigravity returned a JSON response that is not an object")
            if process.returncode or envelope.get("status") != "SUCCESS":
                error = envelope.get("error") or f"Antigravity exited with status {process.returncode}"
                raise RuntimeError(error.replace(api_key, "[redacted]") if api_key else error)
            if not isinstance(envelope.get("structured_output"), dict):
                raise RuntimeError("Antigravity returned no structured diagnosis")
            return envelope
