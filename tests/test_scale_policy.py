from dataclasses import replace

from examples.agents.diagnostic_harness.config import (
    AdaptiveBudgetConfig,
    ScalePolicyConfig,
    TopologyRankerConfig,
)
from examples.agents.diagnostic_harness.topology.scale_policy import TopologyScalePolicy
from tests.test_topology_graph import clos_graph, fat_tree_k12_graph


def _plan(width: int):
    return TopologyScalePolicy().plan(
        clos_graph(width=width),
        adaptive=AdaptiveBudgetConfig(),
        ranker=TopologyRankerConfig(),
    )


def test_scale_policy_uses_local_ecmp_width_for_tiers():
    assert (_plan(4).tier, _plan(4).probe_frontier_limit) == ("small", 8)
    assert (_plan(8).tier, _plan(8).probe_frontier_limit) == ("medium", 16)
    assert (_plan(16).tier, _plan(16).probe_frontier_limit) == ("large", 32)


def test_scale_policy_marks_oversized_local_domain_for_hierarchical_localization():
    plan = _plan(32)

    assert plan.tier == "xlarge"
    assert plan.local_failure_domain_size == 64
    assert plan.probe_frontier_limit == 64
    assert plan.path_sample_limit == 32
    assert not plan.hierarchical_localization


def test_xlarge_path_sampling_grows_only_to_the_configured_cap():
    plan = _plan(64)

    assert plan.path_sample_limit == 64
    assert plan.probe_frontier_limit == 64
    assert plan.hierarchical_localization


def test_scale_policy_counts_multitier_shortest_path_link_domain():
    plan = TopologyScalePolicy().plan(
        fat_tree_k12_graph(),
        adaptive=AdaptiveBudgetConfig(),
        ranker=TopologyRankerConfig(),
    )

    assert plan.tier == "xlarge"
    assert plan.max_ecmp_width == 6
    assert plan.local_failure_domain_size == 84
    assert plan.local_failure_domain_method == "shortest_path_link_union"
    assert plan.probe_frontier_limit == 64
    assert plan.path_sample_limit == 32
    assert plan.hierarchical_localization
    assert plan.frontier_coverage_ratio == 64 / 84
    assert plan.uncovered_failure_domain_candidates == 20


def test_scale_policy_obeys_operator_absolute_safety_ceiling():
    plan = TopologyScalePolicy(replace(ScalePolicyConfig(), xlarge_failure_domain_cap=128)).plan(
        clos_graph(width=32),
        adaptive=replace(AdaptiveBudgetConfig(), max_failure_domain_candidates=24),
        ranker=TopologyRankerConfig(),
    )

    assert plan.probe_frontier_limit == 24
    assert plan.hierarchical_localization


def test_scale_policy_never_changes_submission_thresholds():
    ranker = TopologyRankerConfig()
    TopologyScalePolicy().plan(
        clos_graph(width=16),
        adaptive=AdaptiveBudgetConfig(),
        ranker=ranker,
    )

    assert ranker.candidate_top_k == 6
    assert ranker.candidate_generation_threshold == 0.05


def test_unrelated_client_inventory_does_not_expand_local_probe_frontier():
    graph = clos_graph(width=8)
    baseline = TopologyScalePolicy().plan(
        graph,
        adaptive=AdaptiveBudgetConfig(),
        ranker=TopologyRankerConfig(),
    )
    graph.roles.update({f"unrelated-client-{index}": "client" for index in range(10_000)})

    expanded = TopologyScalePolicy().plan(
        graph,
        adaptive=AdaptiveBudgetConfig(),
        ranker=TopologyRankerConfig(),
    )

    assert expanded.clients == baseline.clients + 10_000
    assert expanded.probe_frontier_limit == baseline.probe_frontier_limit
    assert expanded.route_candidate_limit == baseline.route_candidate_limit
    assert expanded.path_sample_limit == baseline.path_sample_limit
