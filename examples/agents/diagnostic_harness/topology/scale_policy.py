"""Topology-aware scale policy for bounded diagnostic investigations."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import AdaptiveBudgetConfig, ScalePolicyConfig, TopologyRankerConfig
from .graph import TopologyGraph


@dataclass(frozen=True)
class TopologyScalePlan:
    """One immutable execution plan derived from public topology inventory."""

    tier: str
    network_devices: int
    clients: int
    attachment_devices: int
    physical_links: int
    max_ecmp_width: int
    local_failure_domain_size: int
    local_failure_domain_method: str
    probe_frontier_limit: int
    route_candidate_limit: int
    path_sample_limit: int
    hierarchical_localization: bool
    frontier_coverage_ratio: float
    uncovered_failure_domain_candidates: int

    def as_metadata(self) -> dict[str, int | float | str | bool]:
        return asdict(self)


class TopologyScalePolicy:
    """Choose bounded investigation widths without scaling with server count."""

    def __init__(self, config: ScalePolicyConfig | None = None):
        self.config = config or ScalePolicyConfig()

    def plan(
        self,
        graph: TopologyGraph,
        *,
        adaptive: AdaptiveBudgetConfig,
        ranker: TopologyRankerConfig,
    ) -> TopologyScalePlan:
        degree = graph.max_attachment_fabric_degree()
        degree_domain = degree * 2
        path_domain = graph.max_attachment_path_failure_domain_size()
        local_domain = max(degree_domain, path_domain)
        domain_method = "shortest_path_link_union" if path_domain > degree_domain else "two_ended_attachment_cut"
        tier, failure_domain_cap, path_cap = self._tier(degree, local_domain)
        available_fabric_links = len(graph.network_links())

        if not self.config.enabled:
            failure_domain_cap = adaptive.max_failure_domain_candidates
            path_cap = ranker.max_ecmp_paths

        absolute_frontier_cap = min(
            max(0, int(adaptive.max_failure_domain_candidates)),
            max(0, int(failure_domain_cap)),
        )
        desired_frontier = max(
            max(0, int(adaptive.max_ecmp_probe_candidates)),
            local_domain,
        )
        probe_frontier = min(
            desired_frontier,
            absolute_frontier_cap,
            available_fabric_links,
        )
        # A multi-stage Clos/Fat-tree can have many more end-to-end path
        # combinations than attachment uplinks (for example, 6 uplinks but
        # 36 core paths). Never reduce the ranker's established sampling
        # floor; only grow it for a wider local domain, up to the tier cap.
        path_sampling_floor = max(0, int(ranker.max_ecmp_paths))
        path_samples = max(
            path_sampling_floor,
            min(max(path_sampling_floor, int(path_cap)), max(path_sampling_floor, degree)),
        )
        roles = graph.roles.values()
        clients = sum(role == "client" for role in roles)
        network_devices = len(graph.roles) - clients
        route_candidates = min(network_devices, max(min(2, network_devices), probe_frontier))

        return TopologyScalePlan(
            tier=tier,
            network_devices=network_devices,
            clients=clients,
            attachment_devices=len(graph.attachment_devices()),
            physical_links=len(graph.links),
            max_ecmp_width=degree,
            local_failure_domain_size=local_domain,
            local_failure_domain_method=domain_method,
            probe_frontier_limit=probe_frontier,
            route_candidate_limit=route_candidates,
            path_sample_limit=path_samples,
            hierarchical_localization=local_domain > probe_frontier,
            frontier_coverage_ratio=(min(1.0, probe_frontier / local_domain) if local_domain > 0 else 1.0),
            uncovered_failure_domain_candidates=max(0, local_domain - probe_frontier),
        )

    def _tier(self, degree: int, local_domain: int) -> tuple[str, int, int]:
        config = self.config
        if degree <= config.small_max_ecmp_width:
            selected = 0
        elif degree <= config.medium_max_ecmp_width:
            selected = 1
        elif degree <= config.large_max_ecmp_width:
            selected = 2
        else:
            selected = 3
        tiers = (
            ("small", config.small_failure_domain_cap, config.small_path_sample_cap),
            ("medium", config.medium_failure_domain_cap, config.medium_path_sample_cap),
            ("large", config.large_failure_domain_cap, config.large_path_sample_cap),
            ("xlarge", config.xlarge_failure_domain_cap, config.xlarge_path_sample_cap),
        )
        # Multi-tier fabrics can have a narrow attachment degree but a much
        # wider shortest-path link domain. Promote the execution tier until
        # its frontier cap honestly represents that domain.
        while selected < len(tiers) - 1 and local_domain > tiers[selected][1]:
            selected += 1
        return tiers[selected]


__all__ = ["TopologyScalePlan", "TopologyScalePolicy"]
