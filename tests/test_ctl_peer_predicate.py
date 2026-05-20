"""Tests for ``peer()`` CTL predicate sugar — Phase 6 Stage 6-4.2."""

from __future__ import annotations

import pytest

from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify.adapter import TraceMindAdapter
from tm.verify.ctl import parse_expr, parse_predicate_expr
from tm.verify.joint import joint_verify
from tm.verify.state import State


def _noop(ctx):
    return ctx


def _component(marker: str) -> TraceMindAdapter:
    plan = Plan(
        steps={"work": StepSpec(name="work", reads=[], writes=[marker], fn=_noop)},
        rules=[Rule(name="r1", triggers=["start"], steps=["work"])],
    )
    return TraceMindAdapter.from_plan(plan, changed_paths=["start"])


class TestPeerParse:
    def test_peer_has_desugars(self) -> None:
        expr = parse_expr("peer(center, has(quarantined))")
        assert isinstance(expr, type(parse_expr("has(center.quarantined)")))

    def test_peer_has_evaluates_like_namespaced(self) -> None:
        expr_peer = parse_expr("peer(leaf-A, has(downgraded))")
        expr_ns = parse_expr("has(leaf-A.downgraded)")
        st = State(store={"leaf-A.downgraded": True}, pending=(), done=(), events=())
        adapter = TraceMindAdapter.from_plan(Plan(steps={}, rules=[]))
        from tm.verify.ctl import eval_state_expr

        assert eval_state_expr(expr_peer, st, adapter) == eval_state_expr(expr_ns, st, adapter)

    def test_peer_pending(self) -> None:
        expr = parse_predicate_expr("peer(center, pending(work))")
        assert expr.name.lower() == "pending"
        assert expr.value == "center.work"

    def test_peer_done(self) -> None:
        expr = parse_predicate_expr("peer(leaf, done(work))")
        assert expr.value == "leaf.work"

    def test_peer_requires_inner_predicate(self) -> None:
        with pytest.raises(ValueError, match="predicate"):
            parse_expr("peer(center, EX has(x))")

    def test_peer_rejects_unknown_inner(self) -> None:
        with pytest.raises(ValueError, match="has/pending/done"):
            parse_expr("peer(center, unknown(x))")

    def test_peer_in_ag_formula(self) -> None:
        result = joint_verify(
            [_component("quarantined"), _component("downgraded")],
            component_ids=["center", "leaf-A"],
            formulas=["AG !(peer(center, has(quarantined)) && !peer(leaf-A, has(downgraded)))"],
        )
        assert result.verified is False

    def test_peer_ag_over_reachable_states(self) -> None:
        result = joint_verify(
            [_component("quarantined"), _component("downgraded")],
            component_ids=["center", "leaf-A"],
            formulas=["AG !(peer(center, has(quarantined)) && !peer(leaf-A, has(downgraded)))"],
            max_depth=8,
        )
        assert result.state_count >= 4
        assert result.verified is False

    def test_namespaced_and_peer_same_verdict(self) -> None:
        components = [_component("m0"), _component("m1")]
        ids = ["center", "leaf"]
        peer = joint_verify(
            components,
            component_ids=ids,
            formulas=["EF peer(center, has(m0))"],
        )
        namespaced = joint_verify(
            components,
            component_ids=ids,
            formulas=["EF has(center.m0)"],
        )
        assert peer.verdicts[0].satisfied == namespaced.verdicts[0].satisfied

    def test_peer_with_dotted_component_id(self) -> None:
        expr = parse_predicate_expr("peer(bundle.center, has(quarantined))")
        assert expr.value == "bundle.center.quarantined"

    def test_peer_missing_arg_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_expr("peer(center, has())")
