"""Counterexample projection tests — Phase 6 Stage 6-4.3."""

from __future__ import annotations

from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify.adapter import TraceMindAdapter
from tm.verify.explorer import Explorer
from tm.verify.joint import JointAdapter, joint_verify, project_counterexample


def _noop(ctx):
    return ctx


def _component(marker: str) -> TraceMindAdapter:
    plan = Plan(
        steps={"work": StepSpec(name="work", reads=[], writes=[marker], fn=_noop)},
        rules=[Rule(name="r1", triggers=["start"], steps=["work"])],
    )
    return TraceMindAdapter.from_plan(plan, changed_paths=["start"])


class TestProjectCounterexample:
    def test_projection_includes_nodes(self) -> None:
        result = joint_verify(
            [_component("a"), _component("b")],
            component_ids=["center", "leaf"],
            formulas=["AG !has(center.a)"],
        )
        verdict = result.verdicts[0]
        assert not verdict.satisfied
        assert verdict.counterexample
        step = verdict.counterexample[-1]
        assert set(step["nodes"].keys()) == {"center", "leaf"}

    def test_transition_labels_on_steps_after_initial(self) -> None:
        adapter = JointAdapter.from_components(
            [_component("done_marker"), _component("x")],
            component_ids=["center", "leaf"],
        )
        Explorer(adapter).run(max_depth=8)
        result = joint_verify(
            [_component("done_marker"), _component("x")],
            component_ids=["center", "leaf"],
            formulas=["AG !has(center.done_marker)"],
        )
        ce = result.verdicts[0].counterexample
        assert len(ce) >= 2
        assert ce[1]["transition"] is not None
        assert ce[1]["transition"].startswith("center:")

    def test_manual_projection_matches_joint_verify(self) -> None:
        adapter = JointAdapter.from_components([_component("m")], component_ids=["only"])
        explorer = Explorer(adapter)
        model = explorer.run(max_depth=4)
        path = [0, 1] if len(model.states) > 1 else [0]
        projected = project_counterexample(adapter, model, path)
        assert projected[0]["nodes"]["only"]["store"] == {}

    def test_store_snapshot_in_final_step(self) -> None:
        result = joint_verify(
            [_component("flag")],
            component_ids=["node"],
            formulas=["AG !has(node.flag)"],
        )
        final = result.verdicts[0].counterexample[-1]
        assert final["nodes"]["node"]["store"].get("flag") is True

    def test_empty_path_returns_empty(self) -> None:
        adapter = JointAdapter.from_components([_component("x")], component_ids=["n"])
        model = Explorer(adapter).run(max_depth=2)
        assert project_counterexample(adapter, model, []) == []

    def test_state_index_preserved(self) -> None:
        result = joint_verify(
            [_component("x"), _component("y")],
            formulas=["AG !has(agent0.x)"],
        )
        for step in result.verdicts[0].counterexample:
            assert "state_index" in step

    def test_satisfied_formula_has_empty_counterexample(self) -> None:
        result = joint_verify(
            [_component("x")],
            formulas=["EF has(agent0.x)"],
        )
        assert result.verdicts[0].counterexample == []

    def test_three_node_projection(self) -> None:
        result = joint_verify(
            [_component("a"), _component("b"), _component("c")],
            component_ids=["c0", "l1", "l2"],
            formulas=["AG !has(c0.a)"],
            max_depth=12,
        )
        nodes = result.verdicts[0].counterexample[-1]["nodes"]
        assert set(nodes) == {"c0", "l1", "l2"}
