from __future__ import annotations

import pytest

from tm.ana.planner import PlanResult, plan


def test_stats_depth_and_width():
    graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
    result = plan(graph)
    assert isinstance(result, PlanResult)
    assert result.layers == (("a",), ("b", "c"), ("d",))
    assert result.stats.nodes == 4
    assert result.stats.depth == 3
    assert result.stats.max_width == 2


def test_plan_raises_on_cycle():
    with pytest.raises(ValueError):
        plan({"a": ["b"], "b": ["a"]})


def test_plan_infers_missing_nodes():
    result = plan({"root": ["leaf_a", "leaf_b"]})
    assert result.stats.nodes == 3
    assert ("leaf_a", "leaf_b") in result.layers


def test_plan_handles_large_linear_dag():
    size = 10_000
    graph = {f"n{i}": [f"n{i + 1}"] for i in range(size - 1)}
    graph[f"n{size - 1}"] = []
    result = plan(graph)
    assert result.stats.nodes == size
    assert result.stats.depth == size
    assert result.stats.max_width == 1
