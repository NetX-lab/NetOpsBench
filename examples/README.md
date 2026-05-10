# Examples

Self-contained scripts covering the main user workflows. Each one can be run
directly with `PYTHONPATH=. python examples/<script>`.

## Primary workflows

| Script | What it shows |
|--------|---------------|
| `01_run_scenario.py` | Run one generated scenario with automatic provision + teardown |
| `02_run_suite.py` | Run a small fixed suite across worker labs |
| `03_run_scale_benchmark.py` | Run every generated scenario for one scale; supports `--scale`, `--vendor`, `--workers`, and `--repo-root` |

## Advanced workflows

| Script | What it shows |
|--------|---------------|
| `04_custom_faults.py` | Register a custom fault pack, then run a scenario that uses it |
| `05_manual_runtime.py` | Provision a runtime manually and keep it alive for debugging |
| `06_llm_fault_type_judge.py` | Enable optional LLM-as-judge matching for free-form fault type descriptions |

## Supporting directories

- `agents/` — example agent implementation (`MinimalDeepAgent`)
- `faults/` — custom fault extension example (`simple_fault()` API)
- `_common.py` — helper functions shared by the scripts; not public SDK API

## Quick start

First generate the XS benchmark assets from the repo root:

```bash
netopsbench benchmark prepare --scales xs
```

```python
from examples.agents import MinimalDeepAgent
from netopsbench.sdk import NetOpsBench, RunFailedError

with NetOpsBench(workspace=".") as bench:
    agent = bench.agents.wrap(MinimalDeepAgent())
    run = bench.sessions.run_scenario(
        scenario="scenarios/generated/xs/generated_link_down_xs_001.yaml",
        agent=agent,
    )
    try:
        report = run.wait(raise_on_failure=True)
        report.pretty_print()
    except RunFailedError as exc:
        print(f"Run failed: {exc}")
```

The `with` block ensures every wrapped agent (and any other manager
resources) is closed on exit. `wait(raise_on_failure=True)` turns a
failed run into a typed `RunFailedError` so script callers can branch
cleanly instead of inspecting status fields by hand.

Most scripts accept `--vendor` to switch the example agent provider. Set the
matching provider API key in the environment before running real benchmark
sessions.

## Learning path

If you are reading these examples for the first time, follow them in order:

1. **`01_run_scenario.py`** — minimum viable usage of the SDK.
2. **`02_run_suite.py`** — same shape, but a fixed list of scenarios.
3. **`03_run_scale_benchmark.py`** — auto-discover every scenario for one
   topology scale; this is the script most users will run day-to-day.
4. **`04_custom_faults.py`** — extend the platform with your own fault using
   `simple_fault()`.
5. **`05_manual_runtime.py`** — keep a runtime alive across runs for debugging.
6. **`06_llm_fault_type_judge.py`** — opt in to the LLM-as-judge evaluator.

`agents/` and `faults/` are reference *extension points* rather than entry-point
scripts. You can copy them into your own project as templates.

## LLM-as-judge environment variables

Used by `06_llm_fault_type_judge.py` and any session that loads the judge from
`netopsbench.config`:

| Variable | Purpose |
|----------|---------|
| `NETOPSBENCH_FAULT_TYPE_JUDGE_ENABLED` | Set to `1` to enable the judge (default: deterministic only). |
| `NETOPSBENCH_FAULT_TYPE_JUDGE_API_KEY` | API key for the judge LLM (falls back to `OPENAI_API_KEY`). |
| `NETOPSBENCH_FAULT_TYPE_JUDGE_MODEL` | Model id, default `gpt-4o-mini`. |
| `NETOPSBENCH_FAULT_TYPE_JUDGE_BASE_URL` | Override base URL for non-OpenAI providers (DeepSeek, Kimi, ...). |

