"""Tests for non-monotone verify state via ``StepSpec.clears`` — ISSUE-FORMLANG P1.

The Kripke abstraction used to be monotone (``store[w] = True`` only), so a fact
could never become false. ``clears`` lets a step *remove* facts, enabling
release / recover / toggle properties (throttle off, un-quarantine, lease
expiry). The store still ranges over the finite powerset ``2^keys`` so
exploration stays decidable and terminating.

See ``.plan/issues/ISSUE-formal-language-expressiveness.md`` §P1.
"""

from __future__ import annotations

from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify.adapter import TraceMindAdapter
from tm.verify.ctl import check_ctl, parse_expr
from tm.verify.explorer import Explorer


def _noop(ctx):
    return ctx


# ─── adapter-level clear semantics ────────────────────────────────


class TestClearSemantics:
    def test_clear_removes_fact_from_store(self) -> None:
        plan = Plan(
            steps={
                "set": StepSpec(name="set", reads=[], writes=["flag"], fn=_noop),
                "unset": StepSpec(name="unset", reads=["flag"], writes=[], fn=_noop, clears=["flag"]),
            },
            rules=[
                Rule(name="on_start", triggers=["start"], steps=["set"]),
                Rule(name="on_flag", triggers=["flag"], steps=["unset"]),
            ],
        )
        adapter = TraceMindAdapter.from_plan(plan, changed_paths=["start"])
        model = Explorer(adapter).run(max_depth=8)
        stores = [dict(s.store) for s in model.states]
        # Some reachable state has flag set, and some later state has it cleared.
        assert any("flag" in s for s in stores)
        assert any("flag" not in s for s in stores)

    def test_clear_then_write_same_key_ends_cleared(self) -> None:
        # writes apply first, clears second -> overlap resolves to cleared.
        plan = Plan(
            steps={
                "contradict": StepSpec(
                    name="contradict", reads=[], writes=["x"], fn=_noop, clears=["x"]
                ),
            },
            rules=[Rule(name="go", triggers=["start"], steps=["contradict"])],
        )
        adapter = TraceMindAdapter.from_plan(plan, changed_paths=["start"])
        model = Explorer(adapter).run(max_depth=4)
        # No reachable state retains x (it is cleared in the same step).
        assert all("x" not in s.store for s in model.states)


# ─── termination: a toggling loop stays finite in store mode ──────


class TestToggleTerminates:
    def test_set_clear_cycle_is_finite_in_store_mode(self) -> None:
        # A controller that flips 'busy' on and off forever. Under store-hash
        # dedup the reachable store set is finite (2 distinct stores here), so
        # exploration terminates without relying on the depth bound.
        plan = Plan(
            steps={
                "acquire": StepSpec(name="acquire", reads=[], writes=["busy"], fn=_noop),
                "release": StepSpec(name="release", reads=["busy"], writes=[], fn=_noop, clears=["busy"]),
            },
            rules=[
                Rule(name="on_start", triggers=["start"], steps=["acquire"]),
                Rule(name="on_busy", triggers=["busy"], steps=["release"]),
            ],
        )
        adapter = TraceMindAdapter.from_plan(plan, changed_paths=["start"])
        model = Explorer(adapter).run(max_depth=64, hash_mode="store")
        # store ∈ {{}, {busy}} -> at most 2 distinct states; bounded well under depth.
        assert len(model.states) <= 4


# ─── recovery liveness: only provable with clears ─────────────────


class TestRecoveryLiveness:
    """Throttle-release: AG(rate_limited -> AF !rate_limited).

    This formalizes the latent FableNet KPI ``recovery_time_after_throttle`` /
    "限流后系统终将恢复正常吞吐". It is unprovable under a monotone store and
    becomes provable once the recover step can clear ``rate_limited``.
    """

    # p -> q desugars to (!p || q); CTL has no implication operator.
    FORMULA = "AG (!has(rate_limited) || AF !has(rate_limited))"

    def _plan(self, *, recover_clears: bool) -> Plan:
        recover = StepSpec(
            name="recover",
            reads=["rate_limited"],
            writes=["recovered"],
            fn=_noop,
            clears=["rate_limited"] if recover_clears else [],
        )
        return Plan(
            steps={
                "apply_throttle": StepSpec(
                    name="apply_throttle", reads=[], writes=["rate_limited"], fn=_noop
                ),
                "recover": recover,
            },
            rules=[
                Rule(name="on_overload", triggers=["overload"], steps=["apply_throttle"]),
                Rule(name="on_throttle", triggers=["rate_limited"], steps=["recover"]),
            ],
        )

    def _holds(self, *, recover_clears: bool) -> bool:
        adapter = TraceMindAdapter.from_plan(self._plan(recover_clears=recover_clears), changed_paths=["overload"])
        model = Explorer(adapter).run(max_depth=16)
        sat = check_ctl(parse_expr(self.FORMULA), model, adapter)
        return 0 in sat

    def test_recovery_holds_when_throttle_is_cleared(self) -> None:
        assert self._holds(recover_clears=True) is True

    def test_recovery_fails_under_monotone_store(self) -> None:
        # Without clears the throttle is sticky forever -> liveness violated.
        assert self._holds(recover_clears=False) is False
