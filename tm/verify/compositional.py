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
from typing import Dict, List, Tuple

from tm.artifacts.models import AgentBundleBody
from tm.verify.bundle_adapter import adapter_from_bundle
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


__all__ = [
    "AbstractionStats",
    "InterfaceAdapter",
    "interface_adapter_from_contract",
    "interface_atoms",
]
