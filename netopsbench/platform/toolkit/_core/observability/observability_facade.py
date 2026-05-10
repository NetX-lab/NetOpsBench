"""Observability-focused AgentToolkit facade."""

from __future__ import annotations

from .grafana_ops import GrafanaOpsMixin
from .metrics_ops import MetricsOpsMixin
from .pingmesh_ops import PingmeshOpsMixin


class ObservabilityFacadeMixin(GrafanaOpsMixin, MetricsOpsMixin, PingmeshOpsMixin):
    """Thin facade that groups observability-related toolkit capabilities."""

    pass
