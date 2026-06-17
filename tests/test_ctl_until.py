"""Tests for CTL Until operators ``EU(p, q)`` / ``AU(p, q)`` — ISSUE-FORMLANG P2.

Adds the binary Until that completes the CTL adequate set ``{EX, EU, EG}``.
See ``.plan/issues/ISSUE-formal-language-expressiveness.md`` §P2.
"""

from __future__ import annotations

import pytest

from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify.adapter import TraceMindAdapter
from tm.verify.compositional import classify_formula
from tm.verify.ctl import (
    Predicate,
    Until,
    check_ctl,
    eval_state_expr,
    has_ctl_nodes,
    parse_expr,
)
from tm.verify.explorer import ExplorationResult
from tm.verify.state import State


def _noop(ctx):
    return ctx


def _dummy_adapter() -> TraceMindAdapter:
    # check_ctl only consults the adapter for the Terminal predicate, which these
    # tests never use; any well-formed adapter satisfies the type.
    plan = Plan(steps={"x": StepSpec(name="x", reads=[], writes=["x"], fn=_noop)}, rules=[])
    return TraceMindAdapter.from_plan(plan, changed_paths=[])


def _state(*facts: str) -> State:
    return State(store={f: True for f in facts}, pending=(), done=(), events=())


def _model(states, edges) -> ExplorationResult:
    return ExplorationResult(
        states=list(states),
        edges=dict(edges),
        predecessors={},
        deadlocks=[],
        hash_mode="full",
        max_depth=16,
    )


def _check(formula: str, model: ExplorationResult) -> set[int]:
    return check_ctl(parse_expr(formula), model, _dummy_adapter())


# ─── parsing ──────────────────────────────────────────────────────


class TestParsing:
    def test_eu_parses_to_until_node(self) -> None:
        node = parse_expr("EU(has(p), has(q))")
        assert isinstance(node, Until)
        assert node.op == "EU"
        assert isinstance(node.left, Predicate) and node.left.name == "has" and node.left.value == "p"
        assert isinstance(node.right, Predicate) and node.right.value == "q"

    def test_au_parses_to_until_node(self) -> None:
        node = parse_expr("AU(has(p), has(q))")
        assert isinstance(node, Until) and node.op == "AU"

    def test_until_operands_may_be_compound(self) -> None:
        node = parse_expr("EU(has(p) && !has(r), has(q) || has(s))")
        assert isinstance(node, Until)

    @pytest.mark.parametrize(
        "bad",
        [
            "EU has(p)",            # missing '('
            "EU(has(p))",           # only one operand
            "EU(has(p), has(q)",    # missing ')'
            "AU(has(p) has(q))",    # missing comma
        ],
    )
    def test_malformed_until_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_expr(bad)


# ─── semantics: EU vs AU on a branching model ─────────────────────


class TestBranchingSemantics:
    """s0 branches: one path reaches q (s1), one loops on p forever (s2)."""

    def _branch_model(self) -> ExplorationResult:
        # s0={p}  s1={p,q} (terminal)  s2={p} (self-loop, never q)
        states = [_state("p"), _state("p", "q"), _state("p")]
        edges = {0: [1, 2], 2: [2]}
        return _model(states, edges)

    def test_eu_holds_at_branch_root(self) -> None:
        # Some path keeps p until q -> s0 satisfies EU.
        assert 0 in _check("EU(has(p), has(q))", self._branch_model())

    def test_au_fails_at_branch_root(self) -> None:
        # Not all paths reach q (the s2 loop never does) -> s0 violates AU.
        assert 0 not in _check("AU(has(p), has(q))", self._branch_model())

    def test_eu_excludes_diverging_loop_state(self) -> None:
        # s2 can never reach q, so it is in neither EU nor AU.
        model = self._branch_model()
        assert 2 not in _check("EU(has(p), has(q))", model)
        assert 2 not in _check("AU(has(p), has(q))", model)

    def test_q_state_satisfies_both_vacuously(self) -> None:
        model = self._branch_model()
        assert 1 in _check("EU(has(p), has(q))", model)
        assert 1 in _check("AU(has(p), has(q))", model)


class TestUntilGuard:
    def test_eu_false_when_left_absent_at_start(self) -> None:
        # s0 has neither p nor q; even though q is reachable, p must hold first.
        states = [_state(), _state("q")]
        model = _model(states, {0: [1]})
        assert 0 not in _check("EU(has(p), has(q))", model)

    def test_vacuous_true_when_q_holds_now_even_without_p(self) -> None:
        states = [_state("q"), _state("q")]
        model = _model(states, {0: [1]})
        assert 0 in _check("EU(has(p), has(q))", model)
        assert 0 in _check("AU(has(p), has(q))", model)


# ─── reduction laws: EU(true,q)==EF q, AU(true,q)==AF q ────────────


class TestReductionLaws:
    def _linear_then_branch(self) -> ExplorationResult:
        states = [_state("p"), _state("p", "q"), _state("p")]
        edges = {0: [1, 2], 2: [2]}
        return _model(states, edges)

    def test_eu_with_tautology_left_equals_ef(self) -> None:
        model = self._linear_then_branch()
        taut = "(has(p) || !has(p))"
        assert _check(f"EU({taut}, has(q))", model) == _check("EF has(q)", model)

    def test_au_with_tautology_left_equals_af(self) -> None:
        model = self._linear_then_branch()
        taut = "(has(p) || !has(p))"
        assert _check(f"AU({taut}, has(q))", model) == _check("AF has(q)", model)


# ─── integration with the rest of the verifier ───────────────────


class TestNodeIntegration:
    def test_until_is_temporal_node(self) -> None:
        assert has_ctl_nodes(parse_expr("EU(has(p), has(q))")) is True

    def test_until_rejected_in_state_only_eval(self) -> None:
        with pytest.raises(ValueError, match="CTL operator not allowed"):
            eval_state_expr(parse_expr("AU(has(p), has(q))"), _state("p"), _dummy_adapter())

    def test_until_is_out_of_compositional_class(self) -> None:
        # Until is not in the sound A-G fragment -> monolithic (compositional=False).
        cls = classify_formula("EU(has(center.p), has(center.q))", ["center"])
        assert cls.compositional is False

    def test_real_model_end_to_end(self) -> None:
        # Build a real reachable model and check EU against it via check_ctl.
        plan = Plan(
            steps={
                "set_q": StepSpec(name="set_q", reads=[], writes=["q"], fn=_noop),
            },
            rules=[Rule(name="go", triggers=["start"], steps=["set_q"])],
        )
        adapter = TraceMindAdapter.from_plan(plan, initial_store={"p": True}, changed_paths=["start"])
        from tm.verify.explorer import Explorer

        model = Explorer(adapter).run(max_depth=6)
        sat = check_ctl(parse_expr("EU(has(p), has(q))"), model, adapter)
        # The initial state keeps p and reaches q -> it is in the EU sat set.
        assert 0 in sat
