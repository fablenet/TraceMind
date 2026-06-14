"""Interface abstraction over-approximation (Stage 7-V.2).

The ∃-abstraction ``Absᵢ`` must (1) collapse internal leaf state to the interface
alphabet and (2) **simulate** the concrete leaf on that alphabet — every concrete
interface transition is reproduced by the abstraction (no concrete behavior
excluded). That simulation is exactly the over-approximation the safety soundness
argument rests on (``docs/verify/COMPOSITIONAL.md`` §2.4): no false PASS.
"""

from __future__ import annotations

from tm.artifacts.models import AgentBundleBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.compositional import (
    AbstractionStats,
    InterfaceAdapter,
    interface_adapter_from_contract,
    interface_atoms,
)
from tm.verify.explorer import Explorer
from tm.verify.joint import joint_verify


def _bundle(meta_verify: dict) -> AgentBundleBody:
    return AgentBundleBody.from_mapping(
        {"bundle_id": "bundle.leaf", "agents": [], "plan": [], "meta": {"verify": meta_verify}}
    )


# A leaf with internal churn (a → b → emit) culminating in one interface fact.
_CHURN = {
    "initial_store": {},
    "changed_paths": ["start"],
    "steps": {
        "a": {"reads": [], "writes": ["i1"]},
        "b": {"reads": ["i1"], "writes": ["i2"]},
        "emit": {"reads": ["i2"], "writes": ["kpi_ready"]},
    },
    "rules": [
        {"name": "on_start", "triggers": ["start"], "steps": ["a"]},
        {"name": "on_i1", "triggers": ["i1"], "steps": ["b"]},
        {"name": "on_i2", "triggers": ["i2"], "steps": ["emit"]},
    ],
}


# ─── interface alphabet ─────────────────────────────────────────────


def test_interface_atoms_union_kpi_and_guarantees():
    atoms = interface_atoms(["kpi_ready"], ["AG !has(leaf.bad)", "EF has(ready)"])
    assert atoms == ("bad", "kpi_ready", "ready")  # namespaced local key, sorted, deduped


# ─── collapse + hiding ──────────────────────────────────────────────


def test_abstraction_collapses_internal_state():
    abs_leaf = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    stats = abs_leaf.stats
    assert isinstance(stats, AbstractionStats)
    # concrete: {}, {i1}, {i1,i2}, {i1,i2,kpi_ready} = 4 states; abstract: ∅, {kpi_ready} = 2
    assert stats.concrete_state_count == 4
    assert stats.abstract_state_count == 2
    assert stats.abstract_state_count < stats.concrete_state_count


def test_abstraction_hides_internal_facts():
    abs_leaf = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    seen_keys: set[str] = set()
    seen: set[frozenset] = set()
    frontier = [abs_leaf.initial_state()]
    while frontier:
        st = frontier.pop()
        key = frozenset(st.store)
        if key in seen:
            continue
        seen.add(key)
        seen_keys |= set(st.store)
        frontier.extend(nxt for _, nxt in abs_leaf.successors(st))
    assert seen_keys <= {"kpi_ready"}  # i1 / i2 never leak into the abstraction


# ─── the soundness property: simulation / over-approximation ────────


def test_every_concrete_edge_has_an_abstract_edge():
    bundle = _bundle(_CHURN)
    abs_leaf = interface_adapter_from_contract(bundle, kpi_keys=["kpi_ready"])
    atoms = abs_leaf.atoms

    concrete = adapter_from_bundle(bundle)
    result = Explorer(concrete).run(max_depth=16)

    def proj(state):
        return frozenset(k for k in atoms if state.store.get(k))

    for sid, outs in result.edges.items():
        a = proj(result.states[sid])
        for nid in outs:
            b = proj(result.states[nid])
            assert b in abs_leaf.transitions.get(a, ()), f"missing abstract edge {set(a)}→{set(b)}"


def test_clean_termination_is_not_a_deadlock():
    abs_leaf = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    # {kpi_ready} is terminal (no outgoing) but the concrete run completed with an
    # empty pending set, so it is NOT a deadlock (matches TraceMindAdapter semantics).
    final = abs_leaf._state(frozenset({"kpi_ready"}))
    assert abs_leaf.successors(final) == []
    assert abs_leaf.is_deadlocked(final) is False
    assert abs_leaf.is_deadlocked(abs_leaf.initial_state()) is False


def test_genuine_deadlock_is_preserved():
    # ``stuck`` is pending forever: it reads ``never`` which is never written.
    stuck = {
        "initial_store": {},
        "changed_paths": ["start"],
        "steps": {"stuck": {"reads": ["never"], "writes": ["done_x"]}},
        "rules": [{"name": "on_start", "triggers": ["start"], "steps": ["stuck"]}],
    }
    abs_leaf = interface_adapter_from_contract(_bundle(stuck), kpi_keys=["done_x"])
    assert abs_leaf.deadlock_presents == frozenset({frozenset()})
    assert abs_leaf.is_deadlocked(abs_leaf.initial_state()) is True


# ─── determinism ────────────────────────────────────────────────────


def test_abstraction_is_deterministic():
    a = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    b = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    assert a.transitions == b.transitions
    assert a.initial_present == b.initial_present
    assert a.stats.to_dict() == b.stats.to_dict()


# ─── plugs into the existing joint verifier ─────────────────────────


def test_abstract_adapter_composes_in_joint_verify():
    abs_leaf = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])

    reaches = joint_verify([abs_leaf], ["EF has(leaf.kpi_ready)"], component_ids=["leaf"])
    assert reaches.verdicts[0].satisfied is True

    never = joint_verify([abs_leaf], ["AG !has(leaf.kpi_ready)"], component_ids=["leaf"])
    assert never.verdicts[0].satisfied is False


def test_abstract_state_count_matches_joint_exploration():
    abs_leaf = interface_adapter_from_contract(_bundle(_CHURN), kpi_keys=["kpi_ready"])
    report = joint_verify([abs_leaf], ["EF has(leaf.kpi_ready)"], component_ids=["leaf"])
    # solo joint over the abstraction explores exactly its abstract states
    assert report.state_count == abs_leaf.stats.abstract_state_count
