"""Centralized runtime defaults for NetOpsBench."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def repo_root() -> Path:
    """Return the repository root directory.

    Resolution order:
    1. ``NETOPSBENCH_REPO_ROOT`` environment variable (if set).
    2. Walk up from this file: ``netopsbench/config.py`` → repo root (parents[1]).
    """
    env = os.environ.get("NETOPSBENCH_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1]


DEFAULT_GRAFANA_URL = "http://localhost:3000"
DEFAULT_GRAFANA_USER = "admin"
DEFAULT_GRAFANA_PASSWORD = "admin"
DEFAULT_INFLUXDB_URL = "http://localhost:8086"
DEFAULT_INFLUXDB_TOKEN = "replace-me"
DEFAULT_INFLUXDB_ORG = "netopsbench"
DEFAULT_INFLUXDB_BUCKET = "netopsbench"
DEFAULT_AGENT_TIMEOUT_SECONDS = 300
DEFAULT_TELEGRAF_RELOAD_WAIT_SECONDS = 3.0
DEFAULT_WORKER_HEALTH_RETRIES = 12
DEFAULT_WORKER_HEALTH_DELAY_SECONDS = 5
DEFAULT_ACTIVE_INTERFACE_COVERAGE_MIN_RATIO = 0.5
DEFAULT_PINGMESH_INFLUXDB_URL = "http://influxdb:8086"
DEFAULT_SONIC_WAIT_TRIES = 180
DEFAULT_FAULT_TYPE_JUDGE_MODEL = "gpt-4o-mini"


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return max(1, int(str(value).strip()))
    except (ValueError, AttributeError):
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (ValueError, AttributeError):
        return default


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
    """Single source of truth for ``NETOPSBENCH_*`` environment variables.

    Use the module-level :data:`config` singleton everywhere instead of calling
    ``os.environ.get("NETOPSBENCH_…")`` from individual modules. Tests can
    construct a fresh instance to override values without touching the
    process environment.
    """

    grafana_url: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_GRAFANA_URL", DEFAULT_GRAFANA_URL))
    grafana_user: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_GRAFANA_USER", DEFAULT_GRAFANA_USER))
    grafana_password: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_GRAFANA_PASSWORD", DEFAULT_GRAFANA_PASSWORD)
    )
    influxdb_url: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_URL", DEFAULT_INFLUXDB_URL))
    influxdb_token: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_TOKEN", DEFAULT_INFLUXDB_TOKEN)
    )
    influxdb_org: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_ORG", DEFAULT_INFLUXDB_ORG))
    influxdb_bucket: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_INFLUXDB_BUCKET", DEFAULT_INFLUXDB_BUCKET)
    )
    topology_dir: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_TOPOLOGY_DIR"))
    topology_id: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_TOPOLOGY_ID"))
    workspace: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_WORKSPACE"))
    pingmesh_start_time: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_PINGMESH_START_TIME"))
    pingmesh_end_time: str | None = field(default_factory=lambda: os.environ.get("NETOPSBENCH_PINGMESH_END_TIME"))
    agent_timeout_seconds: int = field(
        default_factory=lambda: _parse_int(
            os.environ.get("NETOPSBENCH_AGENT_TIMEOUT_SECONDS"), DEFAULT_AGENT_TIMEOUT_SECONDS
        )
    )
    telegraf_reload_wait_seconds: float = field(
        default_factory=lambda: _parse_float(
            os.environ.get("NETOPSBENCH_TELEGRAF_RELOAD_WAIT_SECONDS"), DEFAULT_TELEGRAF_RELOAD_WAIT_SECONDS
        )
    )
    skip_observability_refresh: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("NETOPSBENCH_SKIP_OBSERVABILITY_REFRESH"), False)
    )
    run_id_suffix: str = field(default_factory=lambda: os.environ.get("NETOPSBENCH_RUN_ID_SUFFIX", ""))
    worker_health_retries: int = field(
        default_factory=lambda: _parse_int(
            os.environ.get("NETOPSBENCH_WORKER_HEALTH_RETRIES"), DEFAULT_WORKER_HEALTH_RETRIES
        )
    )
    worker_health_delay_seconds: int = field(
        default_factory=lambda: _parse_int(
            os.environ.get("NETOPSBENCH_WORKER_HEALTH_DELAY_SECONDS"), DEFAULT_WORKER_HEALTH_DELAY_SECONDS
        )
    )
    active_interface_coverage_min_ratio: float = field(
        default_factory=lambda: _parse_float(
            os.environ.get("NETOPSBENCH_ACTIVE_INTERFACE_COVERAGE_MIN_RATIO"),
            DEFAULT_ACTIVE_INTERFACE_COVERAGE_MIN_RATIO,
        )
    )
    pingmesh_influxdb_url: str = field(
        default_factory=lambda: os.environ.get("NETOPSBENCH_PINGMESH_INFLUXDB_URL", DEFAULT_PINGMESH_INFLUXDB_URL)
    )
    sonic_wait_tries: int = field(
        default_factory=lambda: _parse_int(os.environ.get("NETOPSBENCH_SONIC_WAIT_TRIES"), DEFAULT_SONIC_WAIT_TRIES)
    )
    fault_type_judge_config: FaultTypeJudgeConfig = field(default_factory=FaultTypeJudgeConfig)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def grafana_auth(self) -> tuple:
        """Return ``(user, password)`` tuple for Grafana basic auth."""
        return (self.grafana_user, self.grafana_password)

    def reload(self) -> "NetOpsBenchConfig":
        """Re-read all values from the current process environment.

        Returns ``self`` for chaining. Useful when test fixtures patch
        ``os.environ`` after the module has been imported.
        """
        fresh = NetOpsBenchConfig()
        for key in self.__dataclass_fields__:
            setattr(self, key, getattr(fresh, key))
        return self


config = NetOpsBenchConfig()
