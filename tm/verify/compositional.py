"""Compositional (assume-guarantee) verification — Stage 7-V.2.

This module builds the **interface abstraction** ``Absᵢ`` of a leaf bundle (the
spec's §3): a small over-approximating :class:`ComponentAdapter` that exposes
only the interface facts (the edge ``kpi_keys`` plus atoms referenced by the
leaf's guarantees) and hides all internal state. It is produced by an
**existential (∃) abstraction**: explore the concrete leaf once locally, project
each concrete state onto the interface alphabet, and keep an abstract edge
``a → a'`` whenever *some* concrete edge ``s → s'`` projects to it.

Soundness (the only property we rely on, see ``docs/verify/COMPOSITIONAL.md`` §2.4):
every concrete interface trace is reproduced by the abstraction (the abstract
transition relation simulates the concrete one on the interface alphabet).
Hence a *safety* property that holds on the abstraction holds on the concrete
system — **no false PASS**. The abstraction may admit spurious traces (false
FAIL); those are re-checked monolithically by later tasks (7-V.3/7-V.4).

Deterministic, zero-LLM, no network — like the rest of ``tm/verify``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from tm.artifacts.models import AgentBundleBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.ctl import And, Ctl, Not, Or, Predicate, has_ctl_nodes, parse_expr
from tm.verify.explorer import Explorer
from tm.verify.state import State

#: Matches ``has(<token>)`` atoms inside a CTL guarantee string. The token may be
#: namespaced (``has(leaf.kpi)``); we keep the local key (after the last dot).
_HAS_ATOM = re.compile(r"has\(\s*([A-Za-z0-9_.]+)\s*\)")

Present = frozenset  # an abstract state = the set of interface facts present


def interface_atoms(kpi_keys: List[str], guarantees: List[str] = ()) -> Tuple[str, ...]:
    """Resolve the interface alphabet: ``kpi_keys`` ∪ ``has(...)`` atoms in guarantees.

    Namespaced atoms keep their local key (``has(leaf.ready)`` → ``ready``), so
    the alphabet matches the keys the concrete leaf actually writes to its store.
    Deterministic (sorted, de-duplicated).
    """
    atoms = {str(k) for k in kpi_keys}
    for formula in guarantees or ():
        for token in _HAS_ATOM.findall(str(formula)):
            atoms.add(token.split(".")[-1])
    return tuple(sorted(atoms))


def _present_key(present: Present) -> Tuple[str, ...]:
    return tuple(sorted(present))


def _label(a: Present, b: Present) -> str:
    added = sorted(b - a)
    removed = sorted(a - b)
    if not added and not removed:
        return "abs:stutter"
    parts = [f"+{x}" for x in added] + [f"-{x}" for x in removed]
    return "abs:" + ",".join(parts)


@dataclass(frozen=True)
class AbstractionStats:
    """How much the interface abstraction collapsed the concrete leaf."""

    interface_atoms: Tuple[str, ...]
    concrete_state_count: int
    abstract_state_count: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "interface_atoms": list(self.interface_atoms),
            "concrete_state_count": self.concrete_state_count,
            "abstract_state_count": self.abstract_state_count,
        }


@dataclass
class InterfaceAdapter:
    """Over-approximating :class:`ComponentAdapter` over interface facts only.

    Implements the duck-typed adapter protocol consumed by ``JointAdapter``
    (``initial_state`` / ``successors`` / ``is_deadlocked`` / ``enabled_steps``),
    so it drops into ``joint_verify`` in place of a concrete leaf with zero
    plumbing changes. Abstract states carry a ``store`` holding exactly the
    present interface facts (as ``key: True``), so CTL ``has(<leaf>.<key>)``
    resolves identically to the concrete leaf.
    """

    atoms: Tuple[str, ...]
    initial_present: Present
    transitions: Dict[Present, Tuple[Present, ...]]
    deadlock_presents: frozenset
    stats: AbstractionStats = field(compare=False)

    def _state(self, present: Present) -> State:
        return State(store={k: True for k in sorted(present)}, pending=(), done=(), events=())

    def _present_of(self, state: State) -> Present:
        return frozenset(k for k in self.atoms if state.store.get(k))

    def initial_state(self) -> State:
        return self._state(self.initial_present)

    def successors(self, state: State) -> List[Tuple[str, State]]:
        present = self._present_of(state)
        return [(_label(present, nxt), self._state(nxt)) for nxt in self.transitions.get(present, ())]

    def enabled_steps(self, state: State) -> List[str]:
        present = self._present_of(state)
        return [_label(present, nxt) for nxt in self.transitions.get(present, ())]

    def is_deadlocked(self, state: State) -> bool:
        present = self._present_of(state)
        return present in self.deadlock_presents and not self.transitions.get(present)


def interface_adapter_from_contract(
    bundle: AgentBundleBody,
    *,
    kpi_keys: List[str],
    guarantees: List[str] = (),
    max_depth: int = 16,
    hash_mode: str = "full",
) -> InterfaceAdapter:
    """Build the ∃-abstraction ``Absᵢ`` of ``bundle`` over its interface facts.

    Explores the concrete leaf once (cost ``|Lᵢ|``, not the product), projects
    each state onto :func:`interface_atoms`, and keeps an abstract edge for every
    projected concrete edge. The result over-approximates the leaf's interface
    behavior (see module docstring / spec §2.4) and is typically far smaller.
    """
    atoms = interface_atoms(kpi_keys, guarantees)
    concrete = adapter_from_bundle(bundle)
    result = Explorer(concrete).run(max_depth=max_depth, hash_mode=hash_mode)

    def project(state: State) -> Present:
        return frozenset(k for k in atoms if state.store.get(k))

    proj: List[Present] = [project(s) for s in result.states]

    buckets: Dict[Present, List[Present]] = {}
    present_set: set[Present] = set(proj)
    for sid, outs in result.edges.items():
        a = proj[sid]
        bucket = buckets.setdefault(a, [])
        for nid in outs:
            b = proj[nid]
            if b not in bucket:
                bucket.append(b)

    transitions = {a: tuple(sorted(bucket, key=_present_key)) for a, bucket in buckets.items()}
    deadlock_presents = frozenset(proj[d] for d in result.deadlocks)

    stats = AbstractionStats(
        interface_atoms=atoms,
        concrete_state_count=len(result.states),
        abstract_state_count=len(present_set),
    )
    return InterfaceAdapter(
        atoms=atoms,
        initial_present=proj[0],
        transitions=transitions,
        deadlock_presents=deadlock_presents,
        stats=stats,
    )


# ════════════════════════════════════════════════════════════════════
# 7-V.3 — property classification (the decomposer) + A-G discharge
# ════════════════════════════════════════════════════════════════════
#
# The decomposer decides, *before* verifying, whether a formula is in the
# sound compositional fragment (spec §4). Soundness rests on a single fact
# (spec §2.4): the interface abstraction over-approximates the leaf, so a
# SAFETY property that PASSes on the abstract composition PASSes on the
# concrete system — no false PASS. Everything outside that fragment is
# verified by the existing monolithic product, so the answer is always at
# least as trustworthy as today's.


class PropertyClass(Enum):
    """How a formula may be discharged. Only the first three are compositional."""

    SAFETY_GLOBAL = "safety_global"        # AG p, p CTL-free, multi-component → center A-G discharge
    SINGLE_COMPONENT = "single_component"  # refs one component → exact local verify (no product)
    DECOMPOSABLE = "decomposable"          # AG (∧ of single-component obligations) → per-leaf local
    OUT_OF_CLASS = "out_of_class"          # liveness/existential/nested/coupled → monolithic fallback


def _predicate_component(value: str, component_ids: Sequence[str]) -> Optional[str]:
    """Longest-prefix-match ``value`` to a component id (ids may contain dots)."""
    best: Optional[str] = None
    for cid in component_ids:
        if value == cid or value.startswith(cid + "."):
            if best is None or len(cid) > len(best):
                best = cid
    return best


def referenced_components(expr, component_ids: Sequence[str]) -> frozenset:
    """Set of component ids whose facts ``expr`` references via ``has/pending/done``."""
    out: set[str] = set()

    def walk(e) -> None:
        if isinstance(e, Predicate):
            if e.value is not None:
                cid = _predicate_component(e.value, component_ids)
                if cid is not None:
                    out.add(cid)
        elif isinstance(e, Not):
            walk(e.child)
        elif isinstance(e, (And, Or)):
            walk(e.left)
            walk(e.right)
        elif isinstance(e, Ctl):
            walk(e.child)

    walk(expr)
    return frozenset(out)


def _contains_terminal(expr) -> bool:
    if isinstance(expr, Predicate):
        return expr.name.lower() == "terminal"
    if isinstance(expr, Not):
        return _contains_terminal(expr.child)
    if isinstance(expr, (And, Or)):
        return _contains_terminal(expr.left) or _contains_terminal(expr.right)
    if isinstance(expr, Ctl):
        return _contains_terminal(expr.child)
    return False


def _split_conjuncts(expr) -> List:
    if isinstance(expr, And):
        return _split_conjuncts(expr.left) + _split_conjuncts(expr.right)
    return [expr]


def _unparse(expr) -> str:
    """Serialize an Expr back to formula text (round-trips through ``parse_expr``)."""
    if isinstance(expr, Predicate):
        return expr.name if expr.value is None else f"{expr.name}({expr.value})"
    if isinstance(expr, Not):
        return f"!{_unparse(expr.child)}"
    if isinstance(expr, And):
        return f"({_unparse(expr.left)} && {_unparse(expr.right)})"
    if isinstance(expr, Or):
        return f"({_unparse(expr.left)} || {_unparse(expr.right)})"
    if isinstance(expr, Ctl):
        return f"{expr.op} {_unparse(expr.child)}"
    raise TypeError(f"cannot unparse {expr!r}")


@dataclass(frozen=True)
class Classification:
    formula: str
    property_class: PropertyClass
    components: Tuple[str, ...]
    compositional: bool
    reason: str


def classify_formula(formula: str, component_ids: Sequence[str]) -> Classification:
    """Classify ``formula`` into the compositional fragment (spec §4).

    Sound cases (``compositional=True``):
      * ``AG p`` (p CTL-free) over a single component → exact local discharge;
      * ``AG (c₁ ∧ … ∧ c_k)`` where every conjunct is single-component → decomposable;
      * ``AG p`` over multiple components → A-G discharge against abstractions;
      * ``EF p`` over a single component → exact local reachability.

    Everything else (multi-component liveness/existential, nested CTL, the
    joint-global ``Terminal`` predicate) is ``OUT_OF_CLASS`` → monolithic.
    """
    expr = parse_expr(formula)
    ids = tuple(component_ids)
    refs = tuple(sorted(referenced_components(expr, ids)))

    if _contains_terminal(expr):
        return Classification(
            formula, PropertyClass.OUT_OF_CLASS, refs, False,
            "Terminal is a joint-global predicate; not soundly decomposable",
        )

    if isinstance(expr, Ctl) and expr.op == "AG" and not has_ctl_nodes(expr.child):
        body_refs = referenced_components(expr.child, ids)
        conjuncts = _split_conjuncts(expr.child)
        if len(conjuncts) > 1 and all(len(referenced_components(c, ids)) <= 1 for c in conjuncts):
            return Classification(
                formula, PropertyClass.DECOMPOSABLE, refs, True,
                "AG over a conjunction of single-component obligations (per-leaf local discharge)",
            )
        if len(body_refs) <= 1:
            return Classification(
                formula, PropertyClass.SINGLE_COMPONENT, refs, True,
                "AG safety over a single component (exact local discharge)",
            )
        return Classification(
            formula, PropertyClass.SAFETY_GLOBAL, refs, True,
            "AG safety over multiple components (assume-guarantee discharge)",
        )

    if isinstance(expr, Ctl) and expr.op == "EF" and not has_ctl_nodes(expr.child):
        body_refs = referenced_components(expr.child, ids)
        if len(body_refs) == 1:
            return Classification(
                formula, PropertyClass.SINGLE_COMPONENT, refs, True,
                "EF reachability over a single component (exact local discharge)",
            )
        return Classification(
            formula, PropertyClass.OUT_OF_CLASS, refs, False,
            "existential reachability over multiple components is not over-approximation-sound",
        )

    return Classification(
        formula, PropertyClass.OUT_OF_CLASS, refs, False,
        "liveness/existential/nested CTL is outside the sound compositional fragment",
    )


# ─── A-G discharge ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LeafSpec:
    """A star leaf: its bundle plus the A-G contract on its center-facing edge."""

    component_id: str
    bundle: AgentBundleBody
    kpi_keys: Tuple[str, ...]
    guarantees: Tuple[str, ...] = ()
    assumptions: Tuple[str, ...] = ()

    @classmethod
    def of(cls, component_id, bundle, kpi_keys, guarantees=(), assumptions=()):
        return cls(
            component_id=str(component_id),
            bundle=bundle,
            kpi_keys=tuple(str(k) for k in kpi_keys),
            guarantees=tuple(str(g) for g in guarantees),
            assumptions=tuple(str(a) for a in assumptions),
        )


@dataclass(frozen=True)
class LocalVerdict:
    component_id: str
    guarantee: str
    satisfied: bool


@dataclass(frozen=True)
class Fallback:
    formula: str
    trigger: str  # "out_of_class" | "cyclic_assumption" | "spurious_fail_recheck"
    reason: str
    monolithic_satisfied: bool


@dataclass(frozen=True)
class CompositionalVerdict:
    formula: str
    satisfied: bool
    property_class: str
    via: str  # compositional | compositional_local | compositional_decomposed | monolithic_fallback | monolithic_recheck


@dataclass
class CompositionalReport:
    verified: bool
    center_id: str
    leaf_ids: List[str]
    formulas: List[str]
    verdicts: List[CompositionalVerdict]
    local_verdicts: List[LocalVerdict]
    abstraction_stats: Dict[str, AbstractionStats]
    compositional_state_count: int
    monolithic_state_count: Optional[int]
    fallbacks: List[Fallback]
    mode: str = "compositional"


def _assumption_cycle(leaves: Sequence[LeafSpec], leaf_ids: Sequence[str]) -> Optional[str]:
    """Return the id of a leaf whose assumption references a *sibling* leaf, else None.

    Star A-G is sound only for a two-level, non-circular discharge (spec §2.5): a
    leaf may assume facts about the center, never about another leaf. A sibling
    reference would make the assumption graph cyclic → compositional mode refused.
    """
    siblings = set(leaf_ids)
    for spec in leaves:
        others = siblings - {spec.component_id}
        for a in spec.assumptions:
            try:
                refs = referenced_components(parse_expr(a), leaf_ids)
            except ValueError:
                continue
            if refs & others:
                return spec.component_id
    return None


def assume_guarantee_verify(
    center_bundle: AgentBundleBody,
    leaves: Sequence[LeafSpec],
    formulas: Sequence[str],
    *,
    center_id: str = "center",
    max_depth: int = 16,
    hash_mode: str = "store",
) -> CompositionalReport:
    """Verify ``formulas`` over a star (center + leaves) via assume-guarantee.

    Sound by construction: in-class SAFETY discharges against over-approximating
    abstractions (no false PASS); single-component formulas verify *exactly* on
    the one concrete component; everything else — and any abstract FAIL — is
    (re)verified on the full monolithic product, which stays authoritative.
    """
    from tm.verify.joint import JointAdapter, joint_verify

    center_adapter = adapter_from_bundle(center_bundle)
    leaf_ids = [s.component_id for s in leaves]
    all_ids = [center_id] + leaf_ids
    concrete = {center_id: center_adapter}
    for s in leaves:
        concrete[s.component_id] = adapter_from_bundle(s.bundle)

    abstraction_stats: Dict[str, AbstractionStats] = {}
    abs_components = [center_adapter]
    for s in leaves:
        abs_i = interface_adapter_from_contract(
            s.bundle, kpi_keys=list(s.kpi_keys), guarantees=list(s.guarantees),
            max_depth=max_depth, hash_mode=hash_mode,
        )
        abs_components.append(abs_i)
        abstraction_stats[s.component_id] = abs_i.stats

    # Headline state-count of the abstract composition (formula-independent).
    comp_model = Explorer(
        JointAdapter.from_components(abs_components, component_ids=all_ids)
    ).run(max_depth=max_depth, hash_mode=hash_mode)
    compositional_state_count = len(comp_model.states)

    # Step 1 — local guarantee checks (informational; abstraction is sound regardless).
    local_verdicts: List[LocalVerdict] = []
    for s in leaves:
        for g in s.guarantees:
            rep = joint_verify(
                [concrete[s.component_id]], [g], component_ids=[s.component_id],
                max_depth=max_depth, hash_mode=hash_mode,
            )
            local_verdicts.append(LocalVerdict(s.component_id, g, rep.verdicts[0].satisfied))

    cyclic_leaf = _assumption_cycle(leaves, leaf_ids)
    mono_state: List[Optional[int]] = [None]

    def mono_verify(fstr: str) -> bool:
        rep = joint_verify(
            [concrete[c] for c in all_ids], [fstr], component_ids=all_ids,
            max_depth=max_depth, hash_mode=hash_mode,
        )
        mono_state[0] = rep.state_count
        return rep.verdicts[0].satisfied

    def exact_local(fstr: str, cid: str) -> bool:
        rep = joint_verify(
            [concrete[cid]], [fstr], component_ids=[cid],
            max_depth=max_depth, hash_mode=hash_mode,
        )
        return rep.verdicts[0].satisfied

    def center_verify(fstr: str) -> bool:
        rep = joint_verify(
            abs_components, [fstr], component_ids=all_ids,
            max_depth=max_depth, hash_mode=hash_mode,
        )
        return rep.verdicts[0].satisfied

    verdicts: List[CompositionalVerdict] = []
    fallbacks: List[Fallback] = []

    for f in formulas:
        cls = classify_formula(f, all_ids)
        pc = cls.property_class

        if cls.compositional and cyclic_leaf is not None:
            sat = mono_verify(f)
            verdicts.append(CompositionalVerdict(f, sat, pc.value, "monolithic_fallback"))
            fallbacks.append(Fallback(
                f, "cyclic_assumption",
                f"leaf '{cyclic_leaf}' assumption references a sibling leaf; A-G non-circularity violated",
                sat,
            ))
            continue

        if pc == PropertyClass.OUT_OF_CLASS:
            sat = mono_verify(f)
            verdicts.append(CompositionalVerdict(f, sat, pc.value, "monolithic_fallback"))
            fallbacks.append(Fallback(f, "out_of_class", cls.reason, sat))

        elif pc == PropertyClass.SINGLE_COMPONENT:
            if len(cls.components) != 1:
                sat = mono_verify(f)
                verdicts.append(CompositionalVerdict(f, sat, pc.value, "monolithic_fallback"))
                fallbacks.append(Fallback(
                    f, "out_of_class", "no single component could be resolved for local discharge", sat,
                ))
            else:
                sat = exact_local(f, cls.components[0])
                verdicts.append(CompositionalVerdict(f, sat, pc.value, "compositional_local"))

        elif pc == PropertyClass.DECOMPOSABLE:
            body = parse_expr(f).child  # AG body
            sat = True
            for conj in _split_conjuncts(body):
                crefs = referenced_components(conj, all_ids)
                sub = f"AG {_unparse(conj)}"
                if len(crefs) == 1:
                    sat = exact_local(sub, next(iter(crefs))) and sat
                else:  # 0-ref conjunct cannot be localized → exact on full product
                    sat = mono_verify(sub) and sat
            verdicts.append(CompositionalVerdict(f, sat, pc.value, "compositional_decomposed"))

        else:  # SAFETY_GLOBAL
            sat = center_verify(f)
            if sat:
                verdicts.append(CompositionalVerdict(f, sat, pc.value, "compositional"))
            else:
                # Over-approximation may yield a spurious FAIL → re-check on full product.
                msat = mono_verify(f)
                verdicts.append(CompositionalVerdict(f, msat, pc.value, "monolithic_recheck"))
                fallbacks.append(Fallback(
                    f, "spurious_fail_recheck",
                    "abstract composition reported FAIL; re-verified on the full product (authoritative)",
                    msat,
                ))

    return CompositionalReport(
        verified=all(v.satisfied for v in verdicts),
        center_id=center_id,
        leaf_ids=leaf_ids,
        formulas=list(formulas),
        verdicts=verdicts,
        local_verdicts=local_verdicts,
        abstraction_stats=abstraction_stats,
        compositional_state_count=compositional_state_count,
        monolithic_state_count=mono_state[0],
        fallbacks=fallbacks,
    )


__all__ = [
    "AbstractionStats",
    "Classification",
    "CompositionalReport",
    "CompositionalVerdict",
    "Fallback",
    "InterfaceAdapter",
    "LeafSpec",
    "LocalVerdict",
    "PropertyClass",
    "assume_guarantee_verify",
    "classify_formula",
    "interface_adapter_from_contract",
    "interface_atoms",
    "referenced_components",
]
