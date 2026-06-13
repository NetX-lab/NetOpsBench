# Agent Examples

This directory contains the official public agent examples for NetOpsBench.

## Recommended example

- `minimal_deepagent/agent.py` — a runnable DeepAgent example with MCP tools and multiple provider presets

## File roles

- `minimal_deepagent/agent.py` — self-contained agent with LLM + MCP wiring (DeepAgents, LangChain, provider presets)
- `minimal_deepagent/prompts.py` — system prompt and prompt-building helpers
- `minimal_deepagent/skills/` — example-local DeepAgents skills loaded via `FilesystemBackend`
- `examples.agents.MinimalDeepAgent` re-exports the canonical agent class for stable imports

## Usage pattern

On a fresh clone, generate the XS benchmark assets before using the example
scenario path below:

```bash
netopsbench benchmark prepare --scales xs
```

If you are building against the public SDK API:

1. instantiate your own agent object
2. implement `diagnose(context)` or reuse `MinimalDeepAgent`
3. pass that object into `NetOpsBench(...).sessions.run_scenario(...)`, `run_suite(...)`, or the existing-runtime variants

```python
from examples.agents import MinimalDeepAgent
from netopsbench.sdk import NetOpsBench

with NetOpsBench(workspace=".") as bench:
    agent = bench.agents.wrap(MinimalDeepAgent(vendor="openai"))
    run = bench.sessions.run_scenario(
        scenario="scenarios/generated/xs/generated_link_down_xs_001.yaml",
        agent=agent,
    )
    report = run.wait(raise_on_failure=True)
    report.pretty_print()
```

To switch provider and endpoint explicitly:

```python
agent = MinimalDeepAgent(
    vendor="openai",
    model="gpt-5.5",
    base_url="https://api.openai.com/v1",
)
```

## Runtime requirements

- export the API key for the provider you choose, for example `OPENAI_API_KEY`, `MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`, `ZHIPU_API_KEY`, or `KIMI_API_KEY`
- install the package and optional agent dependencies with `pip install -e ".[agent]"`

For `examples/01_run_scenario.py`, choose provider via CLI argument:

- `PYTHONPATH=. python examples/01_run_scenario.py --vendor openai`
- `PYTHONPATH=. python examples/01_run_scenario.py --vendor minimax`

These examples are references for application code, not required internal platform machinery.
