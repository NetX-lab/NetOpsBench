# Native Codex diagnostic agent

`CodexAgent` runs the actual Codex CLI reasoning and tool loop. It implements
the same `diagnose(context) -> DiagnosisResult` contract as other benchmark
agents. It uses episode-scoped MCP tools and a separate shell container.

From a Linux checkout with Docker and the normal NetOpsBench runtime prerequisites:

```bash
pip install -e '.[codex]'
# Install the native Codex 0.159.2 standalone package, including
# codex-code-mode-host beside the codex executable, then sign in:
codex login
netopsbench benchmark prepare --scales medium
PYTHONPATH=. python examples/01_run_scenario.py --agent codex \
  --scenario scenarios/generated/medium/generated_healthy_network_medium_001.yaml
```

The first run builds `netopsbench-codex:0.159.2` from the installed native
executables. The adapter verifies both the CLI version and required helper.
Selecting Codex does not import DeepAgents or require a DeepSeek API key.

```python
from examples.agents import CodexAgent
from netopsbench.sdk import NetOpsBench

with NetOpsBench(workspace=".") as bench:
    agent = bench.agents.wrap(CodexAgent())
    report = bench.sessions.run_scenario(
        scenario="scenarios/generated/medium/generated_healthy_network_medium_001.yaml",
        agent=agent,
    ).wait(raise_on_failure=True)
```

Defaults are model `gpt-6.1-sol`, effort `xhigh`, explicit Standard service
(`service_tier="default"`, Fast and Ultrafast disabled), a 600-second diagnosis deadline,
40 MCP calls, 2 CPU, 2 GiB RAM and 512 MiB scratch space. Model, effort, CLI
version, image, deadline, MCP budget, auth store and trace directory are explicit
constructor options. DeepAgent retains its existing provider configuration and
graph budget; the reports record the different capabilities and budgets.

Each case starts a fresh ephemeral CLI session and temporary CLI state directory.
Only refreshed `auth.json` persists in `.netopsbench/private/codex-auth/`; the
host login directory, configuration, skills and history are never mounted.
The private credential store is excluded from artifacts. Do not copy it into
reports or an image.

The container receives anonymous public observations, Python and shell tools.
It has no repository, answers, other cases, Docker socket or host privileges.
A namespace firewall is installed before dropping all capabilities. Direct
network access is denied; only the authenticated episode MCP endpoint and an
OpenAI CONNECT relay are reachable. The relay permits `chatgpt.com`,
`api.openai.com` and `auth.openai.com` on port 443, rejects private destinations,
and honors the host HTTPS proxy. Web search, plugins, subagents and bundled
coding skills are disabled. Full-access CLI mode operates inside this container.

MCP and shell actions feed the existing trace recorder; MCP events are counted
once. Redacted native JSONL and stderr are retained separately. Token usage comes
from native completion events. The CLI does not expose an exact model-request
count, so the adapter does not invent one. Nonzero exit, failed turns, timeout,
missing output and invalid JSON produce `success=False` and remain execution
failures rather than completed diagnoses.

Acceptance defaults to DeepAgent with the DeepSeek preset. To explicitly run
both agents for real scale acceptance:

```bash
PYTHONPATH=. python scripts/validate_pingmesh_scales.py \
  --scales fat-tree-k18 --agents deepagent codex --vendor deepseek \
  --output .netopsbench/acceptance-k18
```

The runner preflights both providers and the pinned runtime tools, performs XS
healthy/link/device smoke cases, checks capacity, and runs both K18 campaigns
sequentially in one lab. It saves per-case checkpoints and separate reports.
XS smoke targets a redundant fabric link and a spine, keeping every client
able to send. Access-link outages can cause local send failures and therefore
fail the unchanged complete-observation gate; these attempts remain recorded.
On failure it recovers the injected fault and retains the lab. Failed recovery
or full health quarantines the runtime. Completed low-score diagnoses count as
completed; execution errors are retried on resume.

```bash
PYTHONPATH=. python scripts/validate_pingmesh_scales.py \
  --scales fat-tree-k18 --agents deepagent codex --vendor deepseek \
  --output .netopsbench/acceptance-k18 --resume-runtime /absolute/runtime/path
```

Resume requires the recorded runtime path and unchanged plan, profile, client
image, cases and agent configuration. An already removed runtime can be rebuilt;
quarantined owned resources require explicit `--rebuild-runtime`. External
runtimes are retained even after success. The 12 GiB available-memory emergency
guard may release an owned runtime, with the reason saved in the receipt.

See the official [non-interactive CLI](https://learn.chatgpt.com/docs/non-interactive-mode),
[MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and
[container isolation](https://learn.chatgpt.com/docs/agent-approvals-security) documentation.

Cancellation waits for in-flight container startup before teardown. Container and network removal are verified independently; cleanup failures return an agent execution failure and retain resource identifiers for a later cleanup attempt. Temporary per-case credentials are removed even if Docker cleanup fails.
