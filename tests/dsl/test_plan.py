from __future__ import annotations

import textwrap

from tm.dsl.plan import plan_text, plan_to_dict, plan_to_dot

WDL_SAMPLE = """
version: dsl/v0
workflow: sample
inputs:
  flag: string
steps:
  - first(op.read):
      value: 1
  - when $input.flag in ["YES"]:
      branch(op.write):
        value: $step.first.value
  - finish(op.emit):
      message: $step.branch.value
outputs:
  result: $step.finish
"""


def test_plan_builds_nodes_and_edges() -> None:
    plan = plan_text(textwrap.dedent(WDL_SAMPLE))
    node_ids = {node.id for node in plan.nodes}
    assert {"first", "finish"}.issubset(node_ids)
    assert any(node.type == "condition" for node in plan.nodes)
    assert plan.entry in node_ids or plan.entry.startswith("when_")

    edges = {(edge.source, edge.target) for edge in plan.edges}
    assert ("first", "when_0") in edges or ("when_0", "finish") in edges


def test_plan_to_dot_and_dict() -> None:
    plan = plan_text(textwrap.dedent(WDL_SAMPLE))
    dot = plan_to_dot(plan)
    assert "digraph" in dot
    data = plan_to_dict(plan)
    assert data["workflow"] == "sample"
    assert len(data["nodes"]) == len(plan.nodes)
