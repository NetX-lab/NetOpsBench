"""Centralized runtime defaults for NetOpsBench."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_INFLUXDB_URL = "http://localhost:8086"
DEFAULT_INFLUXDB_TOKEN = "replace-me"
DEFAULT_INFLUXDB_ORG = "netopsbench"
DEFAULT_INFLUXDB_BUCKET = "netopsbench"
DEFAULT_FAULT_TYPE_JUDGE_MODEL = "gpt-4o-mini"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes"}


@dataclass
class FaultTypeJudgeConfig:
    """Configuration for LLM-as-judge fault type evaluation.

    All values are read from environment variables when an instance is created.
    Set ``NETOPSBENCH_FAULT_TYPE_JUDGE_ENABLED=1`` to activate LLM judging;
    by default only deterministic string matching is used.
    """

    enabled: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("NETOPSBENCH_FAULT_TYPE_JUDGE_ENABLED"), False)
    )
    model: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_FAULT_TYPE_JUDGE_MODEL", DEFAULT_FAULT_TYPE_JUDGE_MODEL)
    )
    api_key: str | None = field(
        default_factory=lambda: os.environ.get(
            "NETOPSBENCH_FAULT_TYPE_JUDGE_API_KEY",
            os.environ.get("OPENAI_API_KEY"),
        )
    )
    base_url: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_FAULT_TYPE_JUDGE_BASE_URL"))


@dataclass
class NetOpsBenchConfig:
    """External service configuration read at the process boundary.

    Runtime tuning belongs to scale profiles or subsystem constants. Tests can
    construct a fresh instance to override external service values without
    changing platform domain models.
    """

    influxdb_url: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_URL", DEFAULT_INFLUXDB_URL))
    influxdb_token: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_TOKEN", DEFAULT_INFLUXDB_TOKEN)
    )
    influxdb_org: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_ORG", DEFAULT_INFLUXDB_ORG))
    influxdb_bucket: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_BUCKET", DEFAULT_INFLUXDB_BUCKET)
    )
    topology_dir: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_TOPOLOGY_DIR"))
    fault_type_judge_config: FaultTypeJudgeConfig = field(default_factory=FaultTypeJudgeConfig)


config = NetOpsBenchConfig()
