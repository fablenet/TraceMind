from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Mapping, Sequence, Type, Union

from tm.agents.models import AgentSpec
from tm.artifacts.types import ArtifactType
from tm.controllers.models import EnvSnapshotBody, ExecutionReportBody, ProposedChangePlanBody


class ArtifactStatus(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"


def _require_field(data: Mapping[str, Any], key: str) -> Any:
    if key not in data or data[key] is None:
        raise ValueError(f"missing required field: '{key}'")
    return data[key]


def _ensure_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return dict(value)


def _ensure_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _safe_load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - rare optional dependency
        raise RuntimeError("PyYAML is required to load artifacts") from exc
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, Mapping):
        raise ValueError("artifact payload must be a mapping")
    return raw


@dataclass
class ArtifactEnvelope:
    artifact_id: str
    status: ArtifactStatus
    artifact_type: ArtifactType
    version: str
    created_by: str
    created_at: str
    body_hash: str
    envelope_hash: str
    meta: Dict[str, Any]
    signature: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ArtifactEnvelope":
        _meta = _ensure_dict(_require_field(data, "meta"), "meta")
        return cls(
            artifact_id=_ensure_str(_require_field(data, "artifact_id"), "artifact_id"),
            status=ArtifactStatus(_ensure_str(_require_field(data, "status"), "status")),
            artifact_type=ArtifactType(_ensure_str(_require_field(data, "artifact_type"), "artifact_type")),
            version=_ensure_str(_require_field(data, "version"), "version"),
            created_by=_ensure_str(_require_field(data, "created_by"), "created_by"),
            created_at=_ensure_str(_require_field(data, "created_at"), "created_at"),
            body_hash=_ensure_str(_require_field(data, "body_hash"), "body_hash"),
            envelope_hash=_ensure_str(_require_field(data, "envelope_hash"), "envelope_hash"),
            meta=_meta,
            signature=_ensure_str(data.get("signature"), "signature") if data.get("signature") is not None else None,
        )


def _force_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be a list of strings")
    return [str(item) for item in value]


def _ensure_sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be a sequence")
    return value


def _normalize_slot_fills(value: Any) -> Dict[str, Dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("slot_fills must be a mapping of pattern_id to slot value mapping")
    result: Dict[str, Dict[str, Any]] = {}
    for key, inner in value.items():
        if not isinstance(inner, Mapping):
            raise TypeError(f"slot_fills['{key}'] must be a mapping")
        result[str(key)] = {str(slot_key): slot_value for slot_key, slot_value in inner.items()}
    return result


@dataclass
class TraceLinks:
    parent_intent: str | None = None
    related_intents: List[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "TraceLinks":
        if data is None:
            return cls()
        parent = data.get("parent_intent")
        related = _force_list(data.get("related_intents"), "related_intents")
        if parent is not None and not isinstance(parent, str):
            raise TypeError("trace_links.parent_intent must be a string")
        return cls(parent_intent=str(parent) if parent else None, related_intents=related)


@dataclass
class IntentBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.INTENT
    intent_id: str
    title: str
    context: str
    goal: str
    non_goals: List[str]
    actors: List[str]
    inputs: List[str]
    outputs: List[str]
    constraints: List[str]
    success_metrics: List[str]
    risks: List[str]
    assumptions: List[str]
    trace_links: TraceLinks
    property_pattern_refs: List[str] = field(default_factory=list)
    slot_fills: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntentBody":
        return cls(
            intent_id=_ensure_str(_require_field(data, "intent_id"), "intent_id"),
            title=_ensure_str(_require_field(data, "title"), "title"),
            context=_ensure_str(_require_field(data, "context"), "context"),
            goal=_ensure_str(_require_field(data, "goal"), "goal"),
            non_goals=_force_list(data.get("non_goals"), "non_goals"),
            actors=_force_list(data.get("actors"), "actors"),
            inputs=_force_list(data.get("inputs"), "inputs"),
            outputs=_force_list(data.get("outputs"), "outputs"),
            constraints=_force_list(data.get("constraints"), "constraints"),
            success_metrics=_force_list(data.get("success_metrics"), "success_metrics"),
            risks=_force_list(data.get("risks"), "risks"),
            assumptions=_force_list(data.get("assumptions"), "assumptions"),
            trace_links=TraceLinks.from_mapping(data.get("trace_links")),
            property_pattern_refs=_force_list(data.get("property_pattern_refs"), "property_pattern_refs"),
            slot_fills=_normalize_slot_fills(data.get("slot_fills")),
        )


@dataclass
class CapabilitiesBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.CAPABILITIES
    capability_id: str
    description: str
    inputs: List[str]
    outputs: List[str]
    constraints: List[str]
    execution_binding: Dict[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CapabilitiesBody":
        return cls(
            capability_id=_ensure_str(_require_field(data, "capability_id"), "capability_id"),
            description=_ensure_str(_require_field(data, "description"), "description"),
            inputs=_force_list(data.get("inputs"), "inputs"),
            outputs=_force_list(data.get("outputs"), "outputs"),
            constraints=_force_list(data.get("constraints"), "constraints"),
            execution_binding=_ensure_dict(_require_field(data, "execution_binding"), "execution_binding"),
        )


@dataclass
class PlanStep:
    name: str
    reads: List[str]
    writes: List[str]
    description: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PlanStep":
        return cls(
            name=_ensure_str(_require_field(data, "name"), "name"),
            reads=_force_list(data.get("reads"), "reads"),
            writes=_force_list(data.get("writes"), "writes"),
            description=(
                _ensure_str(data.get("description"), "description") if data.get("description") is not None else None
            ),
        )


@dataclass
class PlanRule:
    name: str
    triggers: List[str]
    steps: List[str]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PlanRule":
        return cls(
            name=_ensure_str(_require_field(data, "name"), "rule.name"),
            triggers=_force_list(data.get("triggers"), "rule.triggers"),
            steps=_force_list(data.get("steps"), "rule.steps"),
        )


@dataclass
class PlanBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.PLAN
    plan_id: str
    owner: str
    summary: str
    steps: List[PlanStep]
    rules: List[PlanRule]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PlanBody":
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, str):
            raise TypeError("steps must be a list of step mappings")
        steps = [PlanStep.from_mapping(_ensure_dict(step, "plan step")) for step in steps_raw]
        rules_raw = data.get("rules") or []
        if rules_raw is None:
            rules_raw = []
        if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, str):
            raise TypeError("rules must be a list of rule mappings")
        rules = [PlanRule.from_mapping(_ensure_dict(rule, "plan rule")) for rule in rules_raw]
        return cls(
            plan_id=_ensure_str(_require_field(data, "plan_id"), "plan_id"),
            owner=_ensure_str(_require_field(data, "owner"), "owner"),
            summary=_ensure_str(_require_field(data, "summary"), "summary"),
            steps=steps,
            rules=rules,
        )


@dataclass
class AgentBundlePlanStep:
    step: str
    agent_id: str
    phase: str | None
    inputs: List[str]
    outputs: List[str]
    description: str | None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentBundlePlanStep":
        raw = _ensure_dict(data, "agent bundle plan step")
        inputs = _force_list(raw.get("inputs"), "plan.inputs")
        outputs = _force_list(raw.get("outputs"), "plan.outputs")
        return cls(
            step=_ensure_str(_require_field(raw, "step"), "plan.step"),
            agent_id=_ensure_str(_require_field(raw, "agent_id"), "plan.agent_id"),
            phase=_ensure_str(raw.get("phase"), "plan.phase") if raw.get("phase") is not None else None,
            inputs=inputs,
            outputs=outputs,
            description=(
                _ensure_str(raw.get("description"), "plan.description") if raw.get("description") is not None else None
            ),
        )


@dataclass
class AgentBundleAgent:
    spec: AgentSpec
    role: str | None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentBundleAgent":
        raw = _ensure_dict(data, "agent bundle agent")
        role_value = raw.get("role")
        spec_data = dict(raw)
        spec_data.pop("role", None)
        spec = AgentSpec.from_mapping(spec_data)
        return cls(
            spec=spec,
            role=_ensure_str(role_value, "agent.role") if role_value is not None else None,
        )


@dataclass
class AgentBundleBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.AGENT_BUNDLE
    bundle_id: str
    agents: List[AgentBundleAgent]
    plan: List[AgentBundlePlanStep]
    meta: Dict[str, Any]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentBundleBody":
        agents_raw = _ensure_sequence(_require_field(data, "agents"), "agents")
        plan_raw = _ensure_sequence(_require_field(data, "plan"), "plan")
        meta_raw = data.get("meta") or {}
        return cls(
            bundle_id=_ensure_str(_require_field(data, "bundle_id"), "bundle_id"),
            agents=[AgentBundleAgent.from_mapping(_ensure_dict(agent, "agent")) for agent in agents_raw],
            plan=[AgentBundlePlanStep.from_mapping(_ensure_dict(step, "plan step")) for step in plan_raw],
            meta=_ensure_dict(meta_raw, "meta"),
        )


@dataclass
class GapMapBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.GAP_MAP
    gap_id: str
    gap_description: str
    impacted_intents: List[str]
    mitigations: List[str]
    severity: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GapMapBody":
        return cls(
            gap_id=_ensure_str(_require_field(data, "gap_id"), "gap_id"),
            gap_description=_ensure_str(_require_field(data, "gap_description"), "gap_description"),
            impacted_intents=_force_list(data.get("impacted_intents"), "impacted_intents"),
            mitigations=_force_list(data.get("mitigations"), "mitigations"),
            severity=_ensure_str(_require_field(data, "severity"), "severity"),
        )


@dataclass
class BacklogItem:
    intent_id: str
    priority: str
    description: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BacklogItem":
        return cls(
            intent_id=_ensure_str(_require_field(data, "intent_id"), "intent_id"),
            priority=_ensure_str(_require_field(data, "priority"), "priority"),
            description=_ensure_str(_require_field(data, "description"), "description"),
        )


@dataclass
class BacklogBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.BACKLOG
    backlog_id: str
    items: List[BacklogItem]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BacklogBody":
        items_raw = data.get("items")
        if not isinstance(items_raw, Sequence) or isinstance(items_raw, str):
            raise TypeError("items must be a list of backlog entries")
        items = [BacklogItem.from_mapping(_ensure_dict(item, "backlog item")) for item in items_raw]
        return cls(
            backlog_id=_ensure_str(_require_field(data, "backlog_id"), "backlog_id"),
            items=items,
        )


@dataclass
class PropertyPatternSlot:
    name: str
    type: str
    description: str | None = None
    required: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PropertyPatternSlot":
        raw = _ensure_dict(data, "property pattern slot")
        required_value = raw.get("required")
        if required_value is None:
            required = True
        elif isinstance(required_value, bool):
            required = required_value
        else:
            raise TypeError("slot.required must be a boolean")
        return cls(
            name=_ensure_str(_require_field(raw, "name"), "slot.name"),
            type=_ensure_str(_require_field(raw, "type"), "slot.type"),
            description=(
                _ensure_str(raw.get("description"), "slot.description") if raw.get("description") is not None else None
            ),
            required=required,
        )


@dataclass
class PropertyPatternCounterexample:
    description: str
    scenario: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PropertyPatternCounterexample":
        raw = _ensure_dict(data, "property pattern counterexample")
        return cls(
            description=_ensure_str(_require_field(raw, "description"), "counterexample.description"),
            scenario=(
                _ensure_str(raw.get("scenario"), "counterexample.scenario") if raw.get("scenario") is not None else None
            ),
        )


@dataclass
class PropertyPatternBody:
    artifact_type: ClassVar[ArtifactType] = ArtifactType.PROPERTY_PATTERN
    pattern_id: str
    category: str
    title: str
    formula_template: str
    slots: List[PropertyPatternSlot]
    description: str | None = None
    applicable_conditions: List[str] = field(default_factory=list)
    counterexamples: List[PropertyPatternCounterexample] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    _CATEGORIES: ClassVar[frozenset[str]] = frozenset({"safety", "liveness", "fairness"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PropertyPatternBody":
        category = _ensure_str(_require_field(data, "category"), "category")
        if category not in cls._CATEGORIES:
            raise ValueError(f"category must be one of {sorted(cls._CATEGORIES)}, got '{category}'")
        slots_raw = _ensure_sequence(_require_field(data, "slots"), "slots")
        if not slots_raw:
            raise ValueError("slots must contain at least one entry")
        slots = [PropertyPatternSlot.from_mapping(slot) for slot in slots_raw]
        counterexamples_raw = data.get("counterexamples") or []
        if not isinstance(counterexamples_raw, Sequence) or isinstance(counterexamples_raw, str):
            raise TypeError("counterexamples must be a sequence")
        counterexamples = [PropertyPatternCounterexample.from_mapping(item) for item in counterexamples_raw]
        metadata_raw = data.get("metadata") or {}
        return cls(
            pattern_id=_ensure_str(_require_field(data, "pattern_id"), "pattern_id"),
            category=category,
            title=_ensure_str(_require_field(data, "title"), "title"),
            formula_template=_ensure_str(_require_field(data, "formula_template"), "formula_template"),
            slots=slots,
            description=(
                _ensure_str(data.get("description"), "description") if data.get("description") is not None else None
            ),
            applicable_conditions=_force_list(data.get("applicable_conditions"), "applicable_conditions"),
            counterexamples=counterexamples,
            metadata=_ensure_dict(metadata_raw, "metadata"),
        )


@dataclass
class KripkeVerdictBody:
    """Declarative form of a Kripke verification verdict (artifact body shape)."""

    verified: bool
    properties_checked: int
    properties_passed: int
    failed_properties: List[str] = field(default_factory=list)
    counterexamples: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "KripkeVerdictBody":
        raw = _ensure_dict(data, "kripke_verdict")
        counterexamples_raw = raw.get("counterexamples") or []
        if not isinstance(counterexamples_raw, Sequence) or isinstance(counterexamples_raw, str):
            raise TypeError("kripke_verdict.counterexamples must be a list")
        return cls(
            verified=bool(_require_field(raw, "verified")),
            properties_checked=int(_require_field(raw, "properties_checked")),
            properties_passed=int(_require_field(raw, "properties_passed")),
            failed_properties=_force_list(raw.get("failed_properties"), "failed_properties"),
            counterexamples=[_ensure_dict(ce, "counterexamples") for ce in counterexamples_raw],
        )


@dataclass
class EvidenceEntryBody:
    """Declarative evidence-chain entry."""

    source: str
    event_type: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EvidenceEntryBody":
        raw = _ensure_dict(data, "evidence_entry")
        ts = raw.get("timestamp")
        return cls(
            source=_ensure_str(_require_field(raw, "source"), "source"),
            event_type=_ensure_str(_require_field(raw, "event_type"), "event_type"),
            data=_ensure_dict(raw.get("data") or {}, "data"),
            timestamp=_ensure_str(ts, "timestamp") if ts is not None else None,
        )


@dataclass
class ProofReportBody:
    """Proof report artifact body — declarative storage form.

    Promoted from runtime ``tm.control.meta.proof.ProofReport`` in Phase 5
    Stage 5-2 task 2.5. The runtime class adds hashing / timestamping; the
    artifact body is the wire/storage form. ``peer_node_id`` and
    ``peer_chain_ref`` are reserved (optional) fields for Phase 6
    AgentNetwork cross-node evidence chains.
    """

    artifact_type: ClassVar[ArtifactType] = ArtifactType.PROOF_REPORT
    report_id: str
    intent_id: str
    cycle_id: str
    overall_verdict: str
    pre_snapshot: Dict[str, Any] = field(default_factory=dict)
    post_snapshot: Dict[str, Any] = field(default_factory=dict)
    execution_summary: Dict[str, Any] = field(default_factory=dict)
    kripke_verdict: KripkeVerdictBody | None = None
    evidence_chain: List[EvidenceEntryBody] = field(default_factory=list)
    policy_decisions: List[Dict[str, Any]] = field(default_factory=list)
    verdict_reason: str | None = None
    created_at: str | None = None
    report_hash: str | None = None
    peer_node_id: str | None = None
    peer_chain_ref: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ProofReportBody":
        evidence_raw = data.get("evidence_chain") or []
        if not isinstance(evidence_raw, Sequence) or isinstance(evidence_raw, str):
            raise TypeError("evidence_chain must be a list")
        policy_raw = data.get("policy_decisions") or []
        if not isinstance(policy_raw, Sequence) or isinstance(policy_raw, str):
            raise TypeError("policy_decisions must be a list")
        kripke_raw = data.get("kripke_verdict")
        kripke = KripkeVerdictBody.from_mapping(kripke_raw) if kripke_raw is not None else None
        return cls(
            report_id=_ensure_str(_require_field(data, "report_id"), "report_id"),
            intent_id=_ensure_str(_require_field(data, "intent_id"), "intent_id"),
            cycle_id=_ensure_str(_require_field(data, "cycle_id"), "cycle_id"),
            overall_verdict=_ensure_str(_require_field(data, "overall_verdict"), "overall_verdict"),
            pre_snapshot=_ensure_dict(data.get("pre_snapshot") or {}, "pre_snapshot"),
            post_snapshot=_ensure_dict(data.get("post_snapshot") or {}, "post_snapshot"),
            execution_summary=_ensure_dict(data.get("execution_summary") or {}, "execution_summary"),
            kripke_verdict=kripke,
            evidence_chain=[EvidenceEntryBody.from_mapping(_ensure_dict(e, "evidence_entry")) for e in evidence_raw],
            policy_decisions=[_ensure_dict(p, "policy_decision") for p in policy_raw],
            verdict_reason=(
                _ensure_str(data.get("verdict_reason"), "verdict_reason")
                if data.get("verdict_reason") is not None
                else None
            ),
            created_at=(
                _ensure_str(data.get("created_at"), "created_at") if data.get("created_at") is not None else None
            ),
            report_hash=(
                _ensure_str(data.get("report_hash"), "report_hash") if data.get("report_hash") is not None else None
            ),
            peer_node_id=(
                _ensure_str(data.get("peer_node_id"), "peer_node_id") if data.get("peer_node_id") is not None else None
            ),
            peer_chain_ref=(
                _ensure_str(data.get("peer_chain_ref"), "peer_chain_ref")
                if data.get("peer_chain_ref") is not None
                else None
            ),
            metadata=_ensure_dict(data.get("metadata") or {}, "metadata"),
        )


@dataclass
class EscalationVerdictBody:
    """Declarative verdict entry inside an EscalationReportBody."""

    kpi: str
    trend: str
    converged: bool | None = None
    delta: float | None = None
    values: List[float] = field(default_factory=list)
    reason: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EscalationVerdictBody":
        raw = _ensure_dict(data, "escalation_verdict")
        values_raw = raw.get("values") or []
        if not isinstance(values_raw, Sequence) or isinstance(values_raw, str):
            raise TypeError("verdict.values must be a list of numbers")
        return cls(
            kpi=_ensure_str(_require_field(raw, "kpi"), "kpi"),
            trend=_ensure_str(_require_field(raw, "trend"), "trend"),
            converged=bool(raw["converged"]) if raw.get("converged") is not None else None,
            delta=float(raw["delta"]) if raw.get("delta") is not None else None,
            values=[float(v) for v in values_raw],
            reason=_ensure_str(raw.get("reason"), "reason") if raw.get("reason") is not None else None,
        )


@dataclass
class EscalationReportBody:
    """L2 escalation artifact body — declarative storage form.

    Promoted from runtime ``tm.control.meta.escalation.EscalationReport``
    in Phase 5 Stage 5-2 task 2.5. ``peer_node_id`` is reserved for Phase 6
    AgentNetwork cross-node escalations.
    """

    artifact_type: ClassVar[ArtifactType] = ArtifactType.ESCALATION_REPORT
    report_id: str
    timestamp: str
    severity: str
    intent_ref: str
    verdicts: List[EscalationVerdictBody] = field(default_factory=list)
    kpi_history_count: int | None = None
    recent_rules_fired: List[str] = field(default_factory=list)
    recent_errors: List[str] = field(default_factory=list)
    gap_summary: str | None = None
    suggested_actions: List[str] = field(default_factory=list)
    counterexample: Dict[str, Any] | None = None
    peer_node_id: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "EscalationReportBody":
        verdicts_raw = data.get("verdicts") or []
        if not isinstance(verdicts_raw, Sequence) or isinstance(verdicts_raw, str):
            raise TypeError("verdicts must be a list")
        suggested_raw = data.get("suggested_actions") or []
        if not isinstance(suggested_raw, Sequence) or isinstance(suggested_raw, str):
            raise TypeError("suggested_actions must be a list")
        counterexample_raw = data.get("counterexample")
        if counterexample_raw is not None and not isinstance(counterexample_raw, Mapping):
            raise TypeError("counterexample must be a mapping or null")
        return cls(
            report_id=_ensure_str(_require_field(data, "report_id"), "report_id"),
            timestamp=_ensure_str(_require_field(data, "timestamp"), "timestamp"),
            severity=_ensure_str(_require_field(data, "severity"), "severity"),
            intent_ref=_ensure_str(_require_field(data, "intent_ref"), "intent_ref"),
            verdicts=[EscalationVerdictBody.from_mapping(_ensure_dict(v, "verdict")) for v in verdicts_raw],
            kpi_history_count=(int(data["kpi_history_count"]) if data.get("kpi_history_count") is not None else None),
            recent_rules_fired=_force_list(data.get("recent_rules_fired"), "recent_rules_fired"),
            recent_errors=_force_list(data.get("recent_errors"), "recent_errors"),
            gap_summary=(
                _ensure_str(data.get("gap_summary"), "gap_summary") if data.get("gap_summary") is not None else None
            ),
            suggested_actions=[_ensure_str(a, "suggested_actions") for a in suggested_raw],
            counterexample=dict(counterexample_raw) if counterexample_raw is not None else None,
            peer_node_id=(
                _ensure_str(data.get("peer_node_id"), "peer_node_id") if data.get("peer_node_id") is not None else None
            ),
            metadata=_ensure_dict(data.get("metadata") or {}, "metadata"),
        )


@dataclass
class AgentNetworkEdge:
    """Edge in an AgentNetwork artifact (star topology v0.3).

    ``from``/``to`` are AgentBundle artifact IDs. ``kpi_keys`` declares the KPIs
    carried over this edge (leaf-to-center: KPIs reported up; center-to-leaf:
    KPIs the leaf must accept as patch payload keys). ``allowed_patches`` is the
    set of patch kinds the *source* may dispatch over this edge — leaves never
    patch the center, so leaf-to-center edges MUST keep ``allowed_patches``
    empty (enforced by lint, not schema).
    """

    source: str
    target: str
    kpi_keys: List[str]
    allowed_patches: List[str] = field(default_factory=list)
    transport: str | None = None
    description: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentNetworkEdge":
        raw = _ensure_dict(data, "agent_network_edge")
        kpi_raw = _ensure_sequence(_require_field(raw, "kpi_keys"), "edge.kpi_keys")
        if not kpi_raw:
            raise ValueError("edge.kpi_keys must contain at least one entry")
        allowed_raw = raw.get("allowed_patches") or []
        if not isinstance(allowed_raw, Sequence) or isinstance(allowed_raw, str):
            raise TypeError("edge.allowed_patches must be a sequence")
        return cls(
            source=_ensure_str(_require_field(raw, "from"), "edge.from"),
            target=_ensure_str(_require_field(raw, "to"), "edge.to"),
            kpi_keys=[_ensure_str(k, "edge.kpi_keys") for k in kpi_raw],
            allowed_patches=[_ensure_str(p, "edge.allowed_patches") for p in allowed_raw],
            transport=(
                _ensure_str(raw.get("transport"), "edge.transport") if raw.get("transport") is not None else None
            ),
            description=(
                _ensure_str(raw.get("description"), "edge.description") if raw.get("description") is not None else None
            ),
        )


@dataclass
class AgentNetworkBody:
    """AgentNetwork artifact body — star-topology agent network description.

    Introduced by K-Ontology v0.3 (Phase 6 Stage 6-1). Describes one center
    AgentBundle aggregating governance over N leaf AgentBundles, with typed
    per-edge contracts (KPIs reported up, patch kinds dispatched down). The
    bundles are referenced by artifact ID — never embedded — following the
    same pattern-by-reference design as v0.2 IntentBody ↔ PropertyPattern.

    v0.3 supports ``topology="star"`` only. ``topology="tree"`` is enum-reserved
    and MUST be rejected by the verifier (see ``_validate_agent_network``).
    """

    artifact_type: ClassVar[ArtifactType] = ArtifactType.AGENT_NETWORK
    network_id: str
    topology: str
    center_bundle_ref: str
    leaf_bundle_refs: List[str]
    edges: List[AgentNetworkEdge]
    transport_default: str
    description: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    _SUPPORTED_TOPOLOGIES: ClassVar[frozenset[str]] = frozenset({"star", "tree"})
    _SUPPORTED_TRANSPORTS: ClassVar[frozenset[str]] = frozenset({"inprocess", "http", "file_queue"})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentNetworkBody":
        topology = _ensure_str(_require_field(data, "topology"), "topology")
        if topology not in cls._SUPPORTED_TOPOLOGIES:
            raise ValueError(f"topology must be one of {sorted(cls._SUPPORTED_TOPOLOGIES)}, got '{topology}'")
        transport_default = _ensure_str(_require_field(data, "transport_default"), "transport_default")
        if transport_default not in cls._SUPPORTED_TRANSPORTS:
            raise ValueError(
                f"transport_default must be one of {sorted(cls._SUPPORTED_TRANSPORTS)}, got '{transport_default}'"
            )
        leaves_raw = _ensure_sequence(_require_field(data, "leaf_bundle_refs"), "leaf_bundle_refs")
        if not leaves_raw:
            raise ValueError("leaf_bundle_refs must contain at least one entry")
        edges_raw = _ensure_sequence(_require_field(data, "edges"), "edges")
        if not edges_raw:
            raise ValueError("edges must contain at least one entry")
        return cls(
            network_id=_ensure_str(_require_field(data, "network_id"), "network_id"),
            topology=topology,
            center_bundle_ref=_ensure_str(_require_field(data, "center_bundle_ref"), "center_bundle_ref"),
            leaf_bundle_refs=[_ensure_str(ref, "leaf_bundle_refs") for ref in leaves_raw],
            edges=[AgentNetworkEdge.from_mapping(_ensure_dict(e, "edge")) for e in edges_raw],
            transport_default=transport_default,
            description=(
                _ensure_str(data.get("description"), "description") if data.get("description") is not None else None
            ),
            metadata=_ensure_dict(data.get("metadata") or {}, "metadata"),
        )


# IntentSession vocabularies. The canonical contract lives in
# ``tm/intent/design_loop.py`` (Task 7-5.0); these frozensets mirror it for
# schema-free validation without importing tm.intent (avoids an import cycle).
# ``tests/test_design_loop_contract.py`` / ``test_intent_session_artifact.py``
# assert they never drift from the design_loop enums.
_SESSION_STATUSES: frozenset[str] = frozenset({"working", "sealed"})
_DESIGN_STEPS: frozenset[str] = frozenset(
    {"draft", "check_5w1h", "propose", "refine", "verify", "accept", "sealed"}
)
_TURN_ROLES: frozenset[str] = frozenset({"human", "agent"})
_TURN_ACTIONS: frozenset[str] = frozenset(
    {"propose", "refine", "check_5w1h", "verify", "accept", "clarify", "note"}
)


@dataclass
class IntentSessionTurn:
    """One append-only journal entry in an IntentSession (Stage 7-2 §2)."""

    seq: int
    role: str
    action: str
    input_ref: str | None = None
    output_ref: str | None = None
    provider: str | None = None
    turn_hash: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntentSessionTurn":
        raw = _ensure_dict(data, "turn")
        seq_value = _require_field(raw, "seq")
        if not isinstance(seq_value, int) or isinstance(seq_value, bool):
            raise TypeError("turn.seq must be an integer")
        role = _ensure_str(_require_field(raw, "role"), "turn.role")
        if role not in _TURN_ROLES:
            raise ValueError(f"turn.role must be one of {sorted(_TURN_ROLES)}, got '{role}'")
        action = _ensure_str(_require_field(raw, "action"), "turn.action")
        if action not in _TURN_ACTIONS:
            raise ValueError(f"turn.action must be one of {sorted(_TURN_ACTIONS)}, got '{action}'")

        def _opt(key: str) -> str | None:
            return _ensure_str(raw.get(key), f"turn.{key}") if raw.get(key) is not None else None

        return cls(
            seq=seq_value,
            role=role,
            action=action,
            input_ref=_opt("input_ref"),
            output_ref=_opt("output_ref"),
            provider=_opt("provider"),
            turn_hash=_opt("turn_hash"),
        )


@dataclass
class IntentSessionSignOff:
    """Accountable seal record (Stage 7-2 §4c). Required when status=sealed."""

    signer: str
    scope: List[str] = field(default_factory=list)
    completeness_snapshot: Dict[str, Any] = field(default_factory=dict)
    dispositions: Dict[str, Any] = field(default_factory=dict)
    gate_report_hash: str | None = None
    signed_at: str | None = None
    sign_hash: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntentSessionSignOff":
        raw = _ensure_dict(data, "sign_off")

        def _opt(key: str) -> str | None:
            return _ensure_str(raw.get(key), f"sign_off.{key}") if raw.get(key) is not None else None

        return cls(
            signer=_ensure_str(_require_field(raw, "signer"), "sign_off.signer"),
            scope=_force_list(raw.get("scope"), "sign_off.scope"),
            completeness_snapshot=_ensure_dict(raw.get("completeness_snapshot") or {}, "sign_off.completeness_snapshot"),
            dispositions=_ensure_dict(raw.get("dispositions") or {}, "sign_off.dispositions"),
            gate_report_hash=_opt("gate_report_hash"),
            signed_at=_opt("signed_at"),
            sign_hash=_opt("sign_hash"),
        )


@dataclass
class IntentSessionBody:
    """IntentSession artifact body — persistent, versioned design journal.

    Introduced by K-Ontology v0.4 (Phase 7 Stage 7-2). A *mutable working*
    record (``status=working``) of how a requirement is iteratively designed:
    an append-only ``turns`` journal, the current design-loop step, and an
    embedded latest 5W1H completeness snapshot. The formal products (Intent /
    PatternInstance / Bundle) are referenced by id and frozen separately —
    byte-identical equivalence is defined on *them*, not on this journal.

    The step / action / status vocabularies are the frozen contract in
    ``tm/intent/design_loop.py`` (Task 7-5.0).
    """

    artifact_type: ClassVar[ArtifactType] = ArtifactType.INTENT_SESSION
    session_id: str
    root_intent_ref: str
    status: str
    current_step: str
    turns: List[IntentSessionTurn]
    completeness: Dict[str, Any] | None = None
    produced_refs: List[str] = field(default_factory=list)
    sign_off: IntentSessionSignOff | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntentSessionBody":
        status = _ensure_str(_require_field(data, "status"), "status")
        if status not in _SESSION_STATUSES:
            raise ValueError(f"status must be one of {sorted(_SESSION_STATUSES)}, got '{status}'")
        current_step = _ensure_str(_require_field(data, "current_step"), "current_step")
        if current_step not in _DESIGN_STEPS:
            raise ValueError(f"current_step must be one of {sorted(_DESIGN_STEPS)}, got '{current_step}'")
        turns_raw = data.get("turns")
        if turns_raw is None:
            turns: List[IntentSessionTurn] = []
        else:
            turns = [IntentSessionTurn.from_mapping(_ensure_dict(t, "turn")) for t in _ensure_sequence(turns_raw, "turns")]
        completeness_raw = data.get("completeness")
        sign_off_raw = data.get("sign_off")
        return cls(
            session_id=_ensure_str(_require_field(data, "session_id"), "session_id"),
            root_intent_ref=_ensure_str(_require_field(data, "root_intent_ref"), "root_intent_ref"),
            status=status,
            current_step=current_step,
            turns=turns,
            completeness=_ensure_dict(completeness_raw, "completeness") if completeness_raw is not None else None,
            produced_refs=_force_list(data.get("produced_refs"), "produced_refs"),
            sign_off=IntentSessionSignOff.from_mapping(sign_off_raw) if sign_off_raw is not None else None,
            metadata=_ensure_dict(data.get("metadata") or {}, "metadata"),
        )


ArtifactBody = Union[
    IntentBody,
    CapabilitiesBody,
    PlanBody,
    GapMapBody,
    BacklogBody,
    AgentBundleBody,
    EnvSnapshotBody,
    ProposedChangePlanBody,
    ExecutionReportBody,
    PropertyPatternBody,
    ProofReportBody,
    EscalationReportBody,
    AgentNetworkBody,
    IntentSessionBody,
]


@dataclass
class Artifact:
    envelope: ArtifactEnvelope
    body: ArtifactBody
    body_raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.envelope.artifact_type != self.body.artifact_type:
            raise ValueError("body artifact type does not match envelope artifact_type")


_BODY_FACTORY: Dict[ArtifactType, Type[ArtifactBody]] = {
    ArtifactType.INTENT: IntentBody,
    ArtifactType.CAPABILITIES: CapabilitiesBody,
    ArtifactType.PLAN: PlanBody,
    ArtifactType.GAP_MAP: GapMapBody,
    ArtifactType.BACKLOG: BacklogBody,
    ArtifactType.AGENT_BUNDLE: AgentBundleBody,
    ArtifactType.ENVIRONMENT_SNAPSHOT: EnvSnapshotBody,
    ArtifactType.PROPOSED_CHANGE_PLAN: ProposedChangePlanBody,
    ArtifactType.EXECUTION_REPORT: ExecutionReportBody,
    ArtifactType.PROPERTY_PATTERN: PropertyPatternBody,
    ArtifactType.PROOF_REPORT: ProofReportBody,
    ArtifactType.ESCALATION_REPORT: EscalationReportBody,
    ArtifactType.AGENT_NETWORK: AgentNetworkBody,
    ArtifactType.INTENT_SESSION: IntentSessionBody,
}


def load_yaml_artifact(path: str | Path) -> Artifact:
    path_obj = Path(path)
    raw = _safe_load_yaml(path_obj)
    envelope_data = raw.get("envelope")
    if envelope_data is None:
        raise ValueError("artifact payload must include an 'envelope' section")
    body_data = raw.get("body")
    if body_data is None:
        raise ValueError("artifact payload must include a 'body' section")
    envelope = ArtifactEnvelope.from_mapping(_ensure_dict(envelope_data, "envelope"))
    _body_raw = _ensure_dict(body_data, "body")
    body_cls = _BODY_FACTORY[envelope.artifact_type]
    body = body_cls.from_mapping(_body_raw)
    return Artifact(envelope=envelope, body=body, body_raw=_body_raw)


__all__ = [
    "Artifact",
    "ArtifactBody",
    "ArtifactEnvelope",
    "ArtifactStatus",
    "ArtifactType",
    "BacklogBody",
    "BacklogItem",
    "CapabilitiesBody",
    "EscalationReportBody",
    "EscalationVerdictBody",
    "EvidenceEntryBody",
    "GapMapBody",
    "IntentBody",
    "IntentSessionBody",
    "IntentSessionSignOff",
    "IntentSessionTurn",
    "KripkeVerdictBody",
    "PlanBody",
    "PlanRule",
    "ProofReportBody",
    "ProposedChangePlanBody",
    "EnvSnapshotBody",
    "ExecutionReportBody",
    "PropertyPatternBody",
    "PropertyPatternCounterexample",
    "PropertyPatternSlot",
    "TraceLinks",
    "AgentBundleAgent",
    "AgentBundleBody",
    "AgentBundlePlanStep",
    "AgentNetworkBody",
    "AgentNetworkEdge",
    "AgentSpec",
    "load_yaml_artifact",
]
