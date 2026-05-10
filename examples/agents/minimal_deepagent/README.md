Minimal DeepAgent Example

This example is kept small on purpose and is organized around three concerns:

- `agent.py`: public example entrypoint and DeepAgent orchestration.
- `providers/`: vendor-specific model factories plus shared runtime/result helpers.
- `prompts.py`, `schema.py`, `skills/`: static example assets.

Layout notes:

- `providers/runtime.py` owns MCP session wiring and runtime trace collection.
- `providers/results.py` owns result parsing, token aggregation, and fallback serialization.
- `agent.py` keeps the example defaults, including `DEFAULT_MAX_TOOL_CALLS = 40`.

This keeps the top-level example readable without leaving unrelated helper logic in one module.
