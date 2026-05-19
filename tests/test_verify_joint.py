"""Tests for ``tm.verify.joint`` — N-arity joint Kripke verification.

Covers:
- JointAdapter construction & component_id semantics
- Asynchronous interleaving (2x, 3x, larger N)
- CTL formula evaluation over joint states (namespaced predicates)
- Pass / fail / violation-path extraction
- Error handling (empty components, malformed ids, lookup outside memo)
- N-arity API: not hardcoded for 2 components
"""

from __future__ import annotations

import pytest

from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify import (
    JointAdapter,
    JointReport,
    JointState,
    JointVerdict,
    joint_verify,
)
from tm.verify.adapter import TraceMindAdapter
from tm.verify.explorer import Explorer
from tm.verify.state import State


def _noop(ctx):
    return ctx


def _trivial_plan(write_marker: str = "done_marker") -> Plan:
    """A single-step plan: rule(start) -> step(work), step writes the marker."""
    return Plan(
        steps={
            "work": StepSpec(name="work", reads=[], writes=[write_marker], fn=_noop),
        },
        rules=[Rule(name="r1", triggers=["start"], steps=["work"])],
    )


def _component(plan: Plan | None = None) -> TraceMindAdapter:
    return TraceMindAdapter.from_plan(plan or _trivial_plan(), changed_paths=["start"])


# ─── JointAdapter construction ─────────────────────────────────────


class TestJointAdapterConstruction:
    def test_default_component_ids(self) -> None:
        adapter = JointAdapter.from_components([_component(), _component()])
        assert adapter.component_ids == ("agent0", "agent1")

    def test_explicit_component_ids(self) -> None:
        adapter = JointAdapter.from_components(
            [_component(), _component()],
            component_ids=["center", "leaf"],
        )
        assert adapter.component_ids == ("center", "leaf")

    def test_empty_components_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one component"):
            JointAdapter.from_components([])

    def test_mismatched_ids_length_rejected(self) -> None:
        with pytest.raises(ValueError, match="length"):
            JointAdapter.from_components(
                [_component(), _component()],
                component_ids=["only-one"],
            )

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            JointAdapter.from_components(
                [_component(), _component()],
                component_ids=["agent", "agent"],
            )

    def test_id_with_dot_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be non-empty"):
            JointAdapter.from_components(
                [_component(), _component()],
                component_ids=["agent.0", "agent.1"],
            )

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be non-empty"):
            JointAdapter.from_components(
                [_component(), _component()],
                component_ids=["", "agent1"],
            )


# ─── JointAdapter Kripke surface ──────────────────────────────────


class TestJointAdapterSurface:
    def test_initial_state_projects_namespaced_pending(self) -> None:
        adapter = JointAdapter.from_components(
            [_component(), _component()],
            component_ids=["a", "b"],
        )
        init = adapter.initial_state()
        assert "a.work" in init.pending
        assert "b.work" in init.pending

    def test_successors_returns_each_component_step(self) -> None:
        adapter = JointAdapter.from_components(
            [_component(), _component()],
            component_ids=["a", "b"],
        )
        init = adapter.initial_state()
        succs = adapter.successors(init)
        labels = [label for label, _ in succs]
        assert any(label.startswith("a:") for label in labels)
        assert any(label.startswith("b:") for label in labels)
        assert len(succs) == 2

    def test_async_interleaving_state_count(self) -> None:
        """N=2 components × 2 reachable per-component states = 4 joint states."""
        adapter = JointAdapter.from_components([_component(), _component()])
        explorer = Explorer(adapter)
        result = explorer.run(max_depth=8)
        assert len(result.states) == 4

    def test_async_interleaving_n3_state_count(self) -> None:
        """N=3 gives 2^3 = 8 reachable states."""
        adapter = JointAdapter.from_components([_component(), _component(), _component()])
        explorer = Explorer(adapter)
        result = explorer.run(max_depth=8)
        assert len(result.states) == 8

    def test_async_interleaving_n5_state_count(self) -> None:
        """N=5 gives 2^5 = 32 reachable states — verifies API is N-arity."""
        adapter = JointAdapter.from_components([_component() for _ in range(5)])
        explorer = Explorer(adapter)
        result = explorer.run(max_depth=16)
        assert len(result.states) == 32

    def test_lookup_unknown_state_raises(self) -> None:
        adapter = JointAdapter.from_components([_component()])
        adapter.initial_state()
        orphan = State(store={}, pending=(), done=(), events=())
        with pytest.raises(KeyError, match="cannot reconstruct"):
            adapter.successors(orphan)

    def test_joint_state_recovery(self) -> None:
        adapter = JointAdapter.from_components(
            [_component(), _component()],
            component_ids=["a", "b"],
        )
        init = adapter.initial_state()
        joint = adapter.joint_state(init)
        assert isinstance(joint, JointState)
        assert len(joint.components) == 2
        assert joint.component_ids == ("a", "b")


# ─── joint_verify end-to-end ──────────────────────────────────────


class TestJointVerifyEndToEnd:
    def test_n2_eventually_both_done(self) -> None:
        result = joint_verify(
            components=[_component(), _component()],
            formulas=["EF (has(agent0.done_marker) && has(agent1.done_marker))"],
        )
        assert isinstance(result, JointReport)
        assert result.verified is True
        assert result.component_count == 2
        assert result.state_count == 4
        assert result.verdicts[0].satisfied is True

    def test_n3_eventually_all_done(self) -> None:
        result = joint_verify(
            components=[_component(), _component(), _component()],
            formulas=[
                "EF (has(agent0.done_marker) && has(agent1.done_marker) && has(agent2.done_marker))",
            ],
        )
        assert result.verified is True
        assert result.component_count == 3
        assert result.state_count == 8

    def test_unsatisfiable_formula_returns_violation_path(self) -> None:
        result = joint_verify(
            components=[_component(), _component()],
            formulas=["EF has(agent0.broken)"],
        )
        assert result.verified is False
        verdict = result.verdicts[0]
        assert verdict.satisfied is False
        assert verdict.violation_path == [0]

    def test_safety_always_holds(self) -> None:
        result = joint_verify(
            components=[_component(), _component()],
            formulas=["AG !has(agent0.broken)"],
        )
        assert result.verified is True

    def test_safety_violation_yields_path(self) -> None:
        # A property that's violated in some reachable state: e.g.
        # AG !has(agent0.done_marker) — fails as soon as agent0 fires
        result = joint_verify(
            components=[_component(), _component()],
            formulas=["AG !has(agent0.done_marker)"],
        )
        assert result.verified is False
        verdict = result.verdicts[0]
        assert verdict.satisfied is False
        assert len(verdict.violation_path) >= 1

    def test_multiple_formulas_mixed_outcomes(self) -> None:
        result = joint_verify(
            components=[_component(), _component()],
            formulas=[
                "EF (has(agent0.done_marker) && has(agent1.done_marker))",
                "EF has(agent0.never_set)",
                "AG !has(agent0.never_set)",
            ],
        )
        assert result.verified is False
        assert result.verdicts[0].satisfied is True
        assert result.verdicts[1].satisfied is False
        assert result.verdicts[2].satisfied is True
        assert result.failed_formulas() == ["EF has(agent0.never_set)"]

    def test_explicit_component_ids_propagate(self) -> None:
        result = joint_verify(
            components=[_component(), _component()],
            component_ids=["center", "leaf"],
            formulas=["EF has(center.done_marker)"],
        )
        assert result.verified is True
        assert result.component_ids == ["center", "leaf"]

    def test_n_arity_phase6_star_topology_smoke(self) -> None:
        """1 center + 4 leaves = N=5 component star, single-formula liveness.

        This is the minimum-viable smoke test for Phase 6 star topology:
        proves the API doesn't hardcode 2 (or 3, or any specific N).
        """
        components = [_component() for _ in range(5)]
        ids = ["center", "leaf-A", "leaf-B", "leaf-C", "leaf-D"]
        result = joint_verify(
            components=components,
            component_ids=ids,
            formulas=[
                "EF (has(center.done_marker) && has(leaf-A.done_marker) "
                "&& has(leaf-B.done_marker) && has(leaf-C.done_marker) "
                "&& has(leaf-D.done_marker))",
            ],
            max_depth=16,
        )
        assert result.verified is True
        assert result.component_count == 5
        assert result.state_count == 32

    def test_single_component_n1_works(self) -> None:
        """N=1 is a degenerate but valid case."""
        result = joint_verify(
            components=[_component()],
            formulas=["EF has(agent0.done_marker)"],
        )
        assert result.verified is True
        assert result.component_count == 1


# ─── JointVerdict dataclass ──────────────────────────────────────


class TestJointVerdictDataclass:
    def test_satisfied_verdict_no_path(self) -> None:
        v = JointVerdict(formula="AG true", satisfied=True)
        assert v.violation_path == []

    def test_failed_formulas_helper(self) -> None:
        report = JointReport(
            verified=False,
            component_count=2,
            component_ids=["a", "b"],
            formulas=["f1", "f2"],
            verdicts=[
                JointVerdict(formula="f1", satisfied=True),
                JointVerdict(formula="f2", satisfied=False, violation_path=[0, 1]),
            ],
            state_count=4,
            edge_count=4,
            deadlock_count=0,
        )
        assert report.failed_formulas() == ["f2"]


# ─── JointState frozen dataclass ─────────────────────────────────


class TestJointStateInvariants:
    def test_length_mismatch_rejected(self) -> None:
        s0 = State(store={}, pending=(), done=(), events=())
        with pytest.raises(ValueError, match="same length"):
            JointState(components=(s0,), component_ids=("a", "b"))

    def test_round_trip(self) -> None:
        s0 = State(store={"x": 1}, pending=(), done=(), events=())
        s1 = State(store={"y": 2}, pending=(), done=(), events=())
        joint = JointState(components=(s0, s1), component_ids=("a", "b"))
        assert joint.components[0].store == {"x": 1}
        assert joint.components[1].store == {"y": 2}
