"""Keep the generated Python config and native Rust parser in lockstep."""

from __future__ import annotations

import re
from pathlib import Path

from netopsbench.platform.client_agent.config import CLIENT_AGENT_SCHEMA_VERSION


def test_python_and_rust_client_agent_schema_versions_match():
    repository = Path(__file__).resolve().parents[1]
    rust_config = (repository / "native/client-agent/src/config.rs").read_text(encoding="utf-8")
    match = re.search(r"pub const CONFIG_SCHEMA_VERSION: u32 = (\d+);", rust_config)

    assert match is not None
    assert int(match.group(1)) == CLIENT_AGENT_SCHEMA_VERSION
