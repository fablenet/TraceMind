"""Assume-guarantee discharge + decomposer (Stage 7-V.3).

Two things are under test:

1. **The decomposer** classifies each formula into the sound compositional
   fragment (spec §4): safety (`AG p`), single-component, decomposable, or
   out-of-class.
2. **`assume_guarantee_verify`** discharges in-class formulas against
   over-approximating abstractions and falls back to the monolithic product for
   everything else — and its per-formula verdict is *always identical* to the
   monolithic verdict (the soundness contract: no false PASS, and any abstract
   FAIL is reconfirmed on the full product).
"""

from __future__ import annotations

from tm.artifacts.models import AgentBundleBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.compositional import (
    LeafSpec,
    PropertyClass,
    assume_guarantee_verify,
    classify_formula,
    referenced_components,
)
from tm.verify.ctl import parse_expr
from tm.verify.joint import joint_verify

IDS = ["center", "leafA", "leafB"]


def _bundle(meta_verify: dict) -> AgentBundleBody:
    return AgentBundleBody.from_mapping(
        {"bundle_id": "b", "agents": [], "plan": [], "meta": {"verify": meta_verify}}
    )


def _churn(kpi: str) -> dict:
    # start → a → b → emit(kpi): three internal states then one interface fact.
    return {
        "initial_store": {},
        "changed_paths": ["start"],
        "steps": {
            "a": {"reads": [], "writes": ["i1"]},
            "b": {"reads": ["i1"], "writes": ["i2"]},
            "emit": {"reads": ["i2"], "writes": [kpi]},
        },
        "rules": [
            {"name": "s", "triggers": ["start"], "steps": ["a"]},
            {"name": "r1", "triggers": ["i1"], "steps": ["b"]},
            {"name": "r2", "triggers": ["i2"], "steps": ["emit"]},
        ],
    }


_CENTER = {
    "initial_store": {},
    "changed_paths": ["start"],
    "steps": {"plan": {"reads": [], "writes": ["planned"]}},
    "rules": [{"name": "s", "triggers": ["start"], "steps": ["plan"]}],
}


def _star(*, cyclic: bool = False):
    center = _bundle(_CENTER)
    la = _bundle(_churn("ready_a"))
    lb = _bundle(_churn("ready_b"))
    leaf_a = LeafSpec.of(
        "leafA", la, ["ready_a"],
        guarantees=["EF has(leafA.ready_a)"],
        assumptions=["AG has(leafB.ready_b)"] if cyclic else (),
    )
    leaf_b = LeafSpec.of("leafB", lb, ["ready_b"])
    return center, [leaf_a, leaf_b], (la, lb)


def _monolithic(center, la, lb, formulas):
    comps = [adapter_from_bundle(center), adapter_from_bundle(la), adapter_from_bundle(lb)]
    return joint_verify(comps, formulas, component_ids=IDS, hash_mode="store")


# ─── the decomposer ─────────────────────────────────────────────────


def test_classification_covers_the_fragment():
    cases = {
        "AG !has(center.bad)": PropertyClass.SINGLE_COMPONENT,
        "AG (!has(leafA.bad_a) && !has(leafB.bad_b))": PropertyClass.DECOMPOSABLE,
        "AG !(has(leafA.ready_a) && has(center.bad))": PropertyClass.SAFETY_GLOBAL,
        "EF has(leafA.ready_a)": PropertyClass.SINGLE_COMPONENT,
        "AF has(center.planned)": PropertyClass.OUT_OF_CLASS,
        "AG Terminal": PropertyClass.OUT_OF_CLASS,
        "EF (has(center.planned) && has(leafA.ready_a))": PropertyClass.OUT_OF_CLASS,
        "AG EF has(center.planned)": PropertyClass.OUT_OF_CLASS,  # nested CTL
    }
    for formula, expected in cases.items():
        cls = classify_formula(formula, IDS)
        assert cls.property_class is expected, f"{formula} → {cls.property_class}"
        assert cls.compositional is (expected is not PropertyClass.OUT_OF_CLASS)


def test_referenced_components_handles_dotted_ids():
    refs = referenced_components(
        parse_expr("peer(bundle.center, has(quarantined))"),
        ["bundle.center", "bundle.leaf_a"],
    )
    assert refs == frozenset({"bundle.center"})


# ─── soundness: compositional verdict == monolithic verdict ─────────


def test_compositional_verdicts_match_monolithic():
    center, leaves, (la, lb) = _star()
    formulas = [
        "AG !(has(leafA.ready_a) && has(center.bad))",      # SAFETY_GLOBAL, true
        "AG (!has(leafA.bad_a) && !has(leafB.bad_b))",      # DECOMPOSABLE, true
        "AG !has(leafA.bad_a)",                              # SINGLE_COMPONENT, true
        "EF has(leafA.ready_a)",                             # SINGLE_COMPONENT, true
        "AF has(center.planned)",                            # OUT_OF_CLASS → fallback
        "AG !(has(leafA.ready_a) && has(center.planned))",  # SAFETY_GLOBAL, false → recheck
    ]
    rep = assume_guarantee_verify(center, leaves, formulas, center_id="center")
    mono = _monolithic(center, la, lb, formulas)

    assert [v.satisfied for v in rep.verdicts] == [v.satisfied for v in mono.verdicts]


def test_in_class_routing_and_no_false_pass():
    center, leaves, _ = _star()
    rep = assume_guarantee_verify(
        center, leaves,
        [
            "AG !(has(leafA.ready_a) && has(center.bad))",
            "AG (!has(leafA.bad_a) && !has(leafB.bad_b))",
            "AG !has(leafA.bad_a)",
            "EF has(leafA.ready_a)",
        ],
        center_id="center",
    )
    vias = {v.formula: v.via for v in rep.verdicts}
    assert vias["AG !(has(leafA.ready_a) && has(center.bad))"] == "compositional"
    assert vias["AG (!has(leafA.bad_a) && !has(leafB.bad_b))"] == "compositional_decomposed"
    assert vias["AG !has(leafA.bad_a)"] == "compositional_local"
    assert vias["EF has(leafA.ready_a)"] == "compositional_local"
    assert rep.verified is True
    assert rep.fallbacks == []  # nothing fell back; all proven compositionally


# ─── fallbacks ──────────────────────────────────────────────────────


def test_out_of_class_falls_back_to_monolithic():
    center, leaves, (la, lb) = _star()
    rep = assume_guarantee_verify(center, leaves, ["AF has(center.planned)"], center_id="center")
    v = rep.verdicts[0]
    assert v.via == "monolithic_fallback"
    assert v.satisfied == _monolithic(center, la, lb, ["AF has(center.planned)"]).verdicts[0].satisfied
    assert rep.fallbacks[0].trigger == "out_of_class"


def test_abstract_fail_is_reconfirmed_on_full_product():
    center, leaves, (la, lb) = _star()
    false_safety = "AG !(has(leafA.ready_a) && has(center.planned))"  # both reachable jointly
    rep = assume_guarantee_verify(center, leaves, [false_safety], center_id="center")
    v = rep.verdicts[0]
    assert v.via == "monolithic_recheck"
    assert v.satisfied is False
    # the monolithic product is authoritative and agrees
    assert _monolithic(center, la, lb, [false_safety]).verdicts[0].satisfied is False
    assert rep.fallbacks[0].trigger == "spurious_fail_recheck"


def test_cyclic_assumption_refuses_compositional_mode():
    center, leaves, _ = _star(cyclic=True)
    rep = assume_guarantee_verify(
        center, leaves, ["AG !(has(leafA.ready_a) && has(center.bad))"], center_id="center",
    )
    v = rep.verdicts[0]
    assert v.via == "monolithic_fallback"
    assert rep.fallbacks[0].trigger == "cyclic_assumption"
    assert "leafA" in rep.fallbacks[0].reason


# ─── headline metrics + bookkeeping ─────────────────────────────────


def test_state_reduction_and_stats():
    center, leaves, (la, lb) = _star()
    rep = assume_guarantee_verify(
        center, leaves, ["AG !(has(leafA.ready_a) && has(center.bad))"], center_id="center",
    )
    mono = _monolithic(center, la, lb, ["AG !has(center.bad)"])
    # center(2) ∥ absA(2) ∥ absB(2) ≪ center(2) ∥ leafA(4) ∥ leafB(4)
    assert rep.compositional_state_count < mono.state_count
    assert set(rep.abstraction_stats) == {"leafA", "leafB"}
    for stats in rep.abstraction_stats.values():
        assert stats.abstract_state_count < stats.concrete_state_count
    assert rep.local_verdicts[0].component_id == "leafA"
    assert rep.local_verdicts[0].satisfied is True


def test_discharge_is_deterministic():
    center, leaves, _ = _star()
    formulas = [
        "AG !(has(leafA.ready_a) && has(center.bad))",
        "AF has(center.planned)",
        "AG !(has(leafA.ready_a) && has(center.planned))",
    ]
    a = assume_guarantee_verify(center, leaves, formulas, center_id="center")
    b = assume_guarantee_verify(center, leaves, formulas, center_id="center")
    assert [(v.satisfied, v.via) for v in a.verdicts] == [(v.satisfied, v.via) for v in b.verdicts]
    assert a.compositional_state_count == b.compositional_state_count
    assert [f.trigger for f in a.fallbacks] == [f.trigger for f in b.fallbacks]
