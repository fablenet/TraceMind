"""Case Corpus — virtual aggregate view over existing artifacts.

Stage 5-4 task 4.2.

A **Case** is a "story" centred on one intent: which artifacts touch it,
what proof was produced, what escalations fired, what change proposals
came up. Cases are not a new artifact kind — they are **materialized
indices** computed from existing :class:`RegistryEntry` records plus the
artifact bodies they point at.

## Why a virtual view, not a new artifact type?

- **Additive change discipline** (Phase 5 invariant 2): adding a new
  artifact kind requires touching K-Ontology schema, validators, AST
  schema, factory tables, and migration paths. A virtual view requires
  none of that.
- **Reproducible from sources**: any consumer (RAG retrieval, dashboard,
  feedback loop) can rebuild the corpus from the registry on demand;
  there's no cache to invalidate.
- **Multiple group-by lenses**: the *same* underlying data is indexed
  both by intent_id (primary) and by pattern_id (secondary, used by
  ``ai.propose_pattern_instances`` RAG).

## Public surface

- :class:`Case` — dataclass aggregating refs + extracted evidence
- :class:`CaseCorpus` — indices built from an :class:`ArtifactRegistry`
- :func:`build_case_corpus` — convenience constructor
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from tm.artifacts import (
    Artifact,
    ArtifactType,
    EscalationReportBody,
    IntentBody,
    ProofReportBody,
    ProposedChangePlanBody,
    PropertyPatternBody,
    RegistryEntry,
)
from tm.artifacts.registry import ArtifactRegistry


ArtifactLoader = Callable[[Path], Artifact]


@dataclass(frozen=True)
class CaseEvidence:
    """A single piece of evidence attached to a Case.

    The ``kind`` reflects the source artifact type (``proof_report``,
    ``escalation_report``, ``proposed_change_plan``); ``ref`` is the
    artifact_id of the source so consumers can trace back.
    """

    kind: str
    ref: str
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    """Aggregate view centred on one intent.

    A Case is keyed by ``intent_id`` (the primary grouping). Each Case
    records:

    - the intent's own RegistryEntry (if found)
    - the set of PropertyPattern ids the intent references
    - all proof / escalation / proposal artifacts touching this intent
    - extracted evidence highlights (counterexamples, verdicts, severities)
      for cheap RAG retrieval

    Mutable so the corpus can fill it in incrementally as it walks the
    registry.
    """

    intent_id: str
    intent_ref: str | None = None
    pattern_refs: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    evidence: List[CaseEvidence] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def evidence_of_kind(self, kind: str) -> List[CaseEvidence]:
        return [item for item in self.evidence if item.kind == kind]

    def has_failures(self) -> bool:
        """True if any attached proof report or escalation report
        indicates failure / regression / critical severity."""
        for ev in self.evidence:
            if ev.kind == "proof_report":
                if ev.details.get("overall_verdict") in {"fail", "inconclusive"}:
                    return True
            if ev.kind == "escalation_report":
                if ev.details.get("severity") in {"warning", "critical"}:
                    return True
        return False


class CaseCorpus:
    """In-memory case index built from an :class:`ArtifactRegistry`.

    Two indices are exposed:

    - ``by_intent_id`` — one :class:`Case` per intent (primary)
    - ``by_pattern_id`` — list of cases that reference a given
      PropertyPattern (secondary; used by RAG)

    Construction is **idempotent and pure**: every call to
    :meth:`build` walks the registry from scratch and rebuilds the indices.
    The corpus does not write any files.
    """

    def __init__(
        self,
        registry: ArtifactRegistry,
        *,
        artifact_loader: ArtifactLoader | None = None,
    ) -> None:
        self._registry = registry
        self._loader = artifact_loader or _default_loader
        self._by_intent: Dict[str, Case] = {}
        self._by_pattern: Dict[str, List[Case]] = {}
        self._unattached: List[CaseEvidence] = []

    @property
    def by_intent_id(self) -> Mapping[str, Case]:
        return self._by_intent

    @property
    def by_pattern_id(self) -> Mapping[str, List[Case]]:
        return self._by_pattern

    @property
    def unattached_evidence(self) -> Sequence[CaseEvidence]:
        """Evidence that could not be associated with any intent.

        Most often this is a ProofReport / EscalationReport whose
        ``intent_id`` / ``intent_ref`` does not yet appear in the
        registry (e.g. because the intent was deleted, or the reports
        were emitted in a different workspace). Surfacing these lets the
        feedback loop (task 4.4) decide whether to drop them or open an
        investigation ticket.
        """
        return tuple(self._unattached)

    def cases(self) -> List[Case]:
        """All known cases, sorted by intent_id for deterministic output."""
        return [self._by_intent[k] for k in sorted(self._by_intent.keys())]

    def cases_for_pattern(self, pattern_id: str) -> List[Case]:
        return list(self._by_pattern.get(pattern_id, []))

    def case_for_intent(self, intent_id: str) -> Optional[Case]:
        return self._by_intent.get(intent_id)

    def build(self) -> "CaseCorpus":
        """Walk the registry and (re)populate both indices."""
        self._by_intent.clear()
        self._by_pattern.clear()
        self._unattached.clear()

        entries = list(self._registry.list_all())

        # First pass: intents (they define the case skeleton)
        for entry in entries:
            if entry.artifact_type != ArtifactType.INTENT:
                continue
            if not entry.intent_id:
                continue
            case = self._ensure_case(entry.intent_id)
            case.intent_ref = entry.artifact_id
            case.artifact_refs.append(entry.artifact_id)
            self._attach_intent_patterns(case, entry)

        # Second pass: every other artifact type contributes evidence
        for entry in entries:
            if entry.artifact_type == ArtifactType.INTENT:
                continue
            handler = _EVIDENCE_HANDLERS.get(entry.artifact_type)
            if handler is None:
                continue
            evidence = handler(entry, self._loader)
            if evidence is None:
                continue
            target_intent = _extract_intent_target(entry, evidence)
            if target_intent and target_intent in self._by_intent:
                case = self._by_intent[target_intent]
                case.evidence.append(evidence)
                case.artifact_refs.append(entry.artifact_id)
            else:
                self._unattached.append(evidence)

        # Build pattern → cases secondary index from completed cases
        for case in self._by_intent.values():
            for pid in case.pattern_refs:
                self._by_pattern.setdefault(pid, []).append(case)

        return self

    def _ensure_case(self, intent_id: str) -> Case:
        case = self._by_intent.get(intent_id)
        if case is None:
            case = Case(intent_id=intent_id)
            self._by_intent[intent_id] = case
        return case

    def _attach_intent_patterns(self, case: Case, entry: RegistryEntry) -> None:
        path = Path(entry.path)
        artifact = _safe_load(self._loader, path)
        if artifact is None:
            return
        if not isinstance(artifact.body, IntentBody):
            return
        pattern_refs = list(artifact.body.property_pattern_refs)
        if pattern_refs:
            case.pattern_refs.extend(pattern_refs)
        # Surface slot_fills in metadata for downstream RAG (lightweight)
        if artifact.body.slot_fills:
            case.metadata.setdefault("slot_fills", dict(artifact.body.slot_fills))


# ─── Evidence handlers ────────────────────────────────────────────


def _proof_report_evidence(entry: RegistryEntry, loader: ArtifactLoader) -> Optional[CaseEvidence]:
    artifact = _safe_load(loader, Path(entry.path))
    if artifact is None or not isinstance(artifact.body, ProofReportBody):
        return None
    body = artifact.body
    counterexamples: List[Mapping[str, Any]] = []
    for ev in body.evidence_chain:
        event_type = (ev.event_type or "").lower()
        if "counterexample" in event_type or "violation" in event_type:
            counterexamples.append(
                {
                    "source": ev.source,
                    "event_type": ev.event_type,
                    "data": dict(ev.data),
                }
            )
    details: Dict[str, Any] = {
        "intent_id": body.intent_id,
        "cycle_id": body.cycle_id,
        "overall_verdict": body.overall_verdict,
        "counterexamples": counterexamples,
    }
    if body.kripke_verdict is not None:
        details["kripke_verdict"] = body.kripke_verdict.verdict
    if body.peer_node_id:
        details["peer_node_id"] = body.peer_node_id
    return CaseEvidence(
        kind="proof_report",
        ref=entry.artifact_id,
        summary=f"proof:{body.report_id} verdict={body.overall_verdict}",
        details=details,
    )


def _escalation_report_evidence(entry: RegistryEntry, loader: ArtifactLoader) -> Optional[CaseEvidence]:
    artifact = _safe_load(loader, Path(entry.path))
    if artifact is None or not isinstance(artifact.body, EscalationReportBody):
        return None
    body = artifact.body
    details: Dict[str, Any] = {
        "intent_ref": body.intent_ref,
        "severity": body.severity,
        "suggested_actions": list(body.suggested_actions),
        "recent_rules_fired": list(body.recent_rules_fired),
        "recent_errors": list(body.recent_errors),
    }
    if body.counterexample is not None:
        details["counterexample"] = dict(body.counterexample)
    if body.peer_node_id:
        details["peer_node_id"] = body.peer_node_id
    return CaseEvidence(
        kind="escalation_report",
        ref=entry.artifact_id,
        summary=f"escalation:{body.report_id} severity={body.severity}",
        details=details,
    )


def _proposed_change_plan_evidence(entry: RegistryEntry, loader: ArtifactLoader) -> Optional[CaseEvidence]:
    artifact = _safe_load(loader, Path(entry.path))
    if artifact is None or not isinstance(artifact.body, ProposedChangePlanBody):
        return None
    body = artifact.body
    details: Dict[str, Any] = {
        "intent_id": body.intent_id,
        "decisions": [
            {
                "effect_ref": d.effect_ref,
                "idempotency_key": d.idempotency_key,
            }
            for d in body.decisions
        ],
        "summary": body.summary,
        "policy_requirements": list(body.policy_requirements),
    }
    return CaseEvidence(
        kind="proposed_change_plan",
        ref=entry.artifact_id,
        summary=f"proposal:{body.plan_id} for intent={body.intent_id}",
        details=details,
    )


def _property_pattern_evidence(entry: RegistryEntry, loader: ArtifactLoader) -> Optional[CaseEvidence]:
    """PropertyPattern artifacts are themselves library entries; they
    contribute "definition" evidence to the case keyed on their first
    referencing intent. Returns None for now — patterns surface via the
    intent's ``property_pattern_refs`` list, not as standalone evidence.
    """
    _ = (entry, loader)
    return None


def _consistency_report_evidence(entry: RegistryEntry, loader: ArtifactLoader) -> Optional[CaseEvidence]:
    """Stub for future ConsistencyReport-as-artifact promotion.

    Currently ConsistencyReports are not stored as artifacts (they are
    runtime-only); this handler is reserved for the day they become
    K-Ontology artifacts. Returns None for now.
    """
    _ = (entry, loader)
    return None


_EVIDENCE_HANDLERS: Dict[ArtifactType, Callable[[RegistryEntry, ArtifactLoader], Optional[CaseEvidence]]] = {
    ArtifactType.PROOF_REPORT: _proof_report_evidence,
    ArtifactType.ESCALATION_REPORT: _escalation_report_evidence,
    ArtifactType.PROPOSED_CHANGE_PLAN: _proposed_change_plan_evidence,
    ArtifactType.PROPERTY_PATTERN: _property_pattern_evidence,
}


def _extract_intent_target(entry: RegistryEntry, evidence: CaseEvidence) -> str | None:
    """Find the intent_id this piece of evidence belongs to.

    Three places to look (in priority order):

    1. The RegistryEntry's own ``intent_id`` field (populated for
       artifacts whose body has an ``intent_id`` attribute)
    2. ``evidence.details["intent_id"]`` (proof reports, proposals)
    3. ``evidence.details["intent_ref"]`` (escalation reports)
    """
    if entry.intent_id:
        return entry.intent_id
    if "intent_id" in evidence.details:
        value = evidence.details["intent_id"]
        if isinstance(value, str) and value:
            return value
    if "intent_ref" in evidence.details:
        value = evidence.details["intent_ref"]
        if isinstance(value, str) and value:
            return value
    return None


# ─── Defaults / helpers ───────────────────────────────────────────


def _default_loader(path: Path) -> Artifact:
    """Default loader: ``tm.artifacts.load_yaml_artifact``."""
    from tm.artifacts import load_yaml_artifact

    return load_yaml_artifact(path)


def _safe_load(loader: ArtifactLoader, path: Path) -> Optional[Artifact]:
    if not path.exists():
        return None
    try:
        return loader(path)
    except Exception:
        return None


def build_case_corpus(
    registry: ArtifactRegistry,
    *,
    artifact_loader: ArtifactLoader | None = None,
) -> CaseCorpus:
    """Convenience: build and populate a :class:`CaseCorpus` in one call."""
    return CaseCorpus(registry, artifact_loader=artifact_loader).build()


# Make linter happy when these types are referenced via Mapping[Any, Any].
_ = (PropertyPatternBody,)


__all__ = [
    "Case",
    "CaseCorpus",
    "CaseEvidence",
    "build_case_corpus",
]
