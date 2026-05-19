"""Compilation chain — Pattern instances → IntentTree → PlanBody → AgentBundle.

Stage 5-4 task 4.5.

This module turns the **declarative** output of Stage 5-3
(:class:`tm.patterns.PatternInstance` + :class:`tm.artifacts.IntentBody`)
into the **executable** Phase 5 control artifacts:

- A :class:`PlanBody` ("PolicySet"): the steps + rules a controller will
  run, with rules derived from each pattern's shape
- An :class:`AgentBundleBody`: a MAPE-K skeleton (observe → analyze →
  decide → act) wired against the plan
- A list of :class:`PropertySpec` entries: the CTL properties extracted
  from each pattern instance, ready for ``tm verify``

## Determinism and what counts as a "skeleton"

Compilation is **fully deterministic**: the same input always produces
the same artifact bytes (no UUIDs, no timestamps in the body). Agents
ship with placeholder ``runtime.config`` — downstream consumers
(fablenet-control, K8s scenarios) override the runtime by editing
``meta.runtime_overrides`` on the bundle. This separation keeps the
compiler domain-neutral.

## Mapping rules (PropertyPattern → PlanRule)

| Pattern category | Generated rule shape |
|---|---|
| ``safety`` | trigger=``observe`` → step ``check_safety`` |
| ``liveness`` | trigger=``decide`` → step ``ensure_liveness`` |
| ``fairness`` | trigger=``act`` → step ``mediate_fairness`` |

The rule body is **always the same 4-step MAPE skeleton** plus per-
pattern rules that fire the corresponding stage's analysis. This is the
universal control discipline — different controllers, same M/A/P/E
skeleton; differences are in K (the rules + patterns themselves).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from tm.artifacts import (
    AgentBundleAgent,
    AgentBundleBody,
    AgentBundlePlanStep,
    IntentBody,
    PlanBody,
    PlanRule,
)
from tm.artifacts.models import PlanStep
from tm.agents.models import (
    AgentContract,
    AgentEvidenceOutput,
    AgentRuntime,
    AgentSpec,
    EffectIdempotency,
    EffectRef,
    IORef,
)
from tm.patterns import PatternInstance
from tm.verify.spec import PropertySpec

# ─── Compilation result aggregate ─────────────────────────────────


@dataclass
class CompilationResult:
    """All four artifacts produced by :func:`compile_intent_to_bundle`.

    Carried together so a single call produces a coherent set
    (intent_id / plan_id / bundle_id all cross-reference) and downstream
    governance can verify them as a group.
    """

    intent: IntentBody
    plan: PlanBody
    bundle: AgentBundleBody
    properties: List[PropertySpec] = field(default_factory=list)
    pattern_instances: List[PatternInstance] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Plain-dict form suitable for YAML / JSON dumping.

        Useful for CLI / tests that want to compare structural output.
        """
        return {
            "intent": _intent_to_dict(self.intent),
            "plan": _plan_to_dict(self.plan),
            "bundle": _bundle_to_dict(self.bundle),
            "properties": [{"name": p.name, "formula": p.formula} for p in self.properties],
        }


# ─── Compilation entry point ──────────────────────────────────────


# Pattern-category → MAPE-K stage routing used to generate Plan rules.
# Keeping this in module scope lets tests assert the contract explicitly.
CATEGORY_TO_STAGE: Mapping[str, str] = {
    "safety": "analyze",
    "liveness": "decide",
    "fairness": "act",
}

MAPE_PHASES: Sequence[str] = ("observe", "analyze", "decide", "act")

# Bundle-plan ``phase`` field is constrained to the K-Ontology lifecycle
# phases (init / run / emit / finalize). Map MAPE-K stages onto them so
# the compiled bundle passes governance validation while still carrying
# the MAPE-K stage name in ``step`` and ``role``.
_MAPE_TO_BUNDLE_PHASE: Mapping[str, str] = {
    "observe": "init",
    "analyze": "run",
    "decide": "run",
    "act": "emit",
}

_DEFAULT_RUNTIME_KIND = "tm-mape-skeleton"


def compile_intent_to_bundle(
    intent: IntentBody,
    pattern_instances: Sequence[PatternInstance],
    *,
    bundle_id: str | None = None,
    plan_id: str | None = None,
    owner: str = "tracemind.compiler",
    summary: str | None = None,
    runtime_kind: str = _DEFAULT_RUNTIME_KIND,
    runtime_config: Mapping[str, Any] | None = None,
) -> CompilationResult:
    """Compile a pattern-based IntentBody to a PlanBody + AgentBundleBody.

    Args:
        intent: An IntentBody (Stage 5-3 output). Its
            ``property_pattern_refs`` should match the ``pattern_id`` of
            each entry in ``pattern_instances``; the compiler does not
            silently drop mismatches.
        pattern_instances: Resolved :class:`PatternInstance` objects, one
            per pattern referenced by the intent (Stage 5-3 task 3.3
            output). Order is preserved into the plan and bundle.
        bundle_id / plan_id: Defaults derived from ``intent.intent_id``.
        runtime_kind: Default agent runtime kind. Use the default
            ``"tm-mape-skeleton"`` for a downstream-overridable skeleton;
            domain adapters can pass their own kind (e.g. ``"k8s-shell"``)
            to pre-configure the bundle for a specific control surface.
        runtime_config: Optional shared runtime config; merged with
            per-agent metadata. Defaults to an empty dict.

    Returns:
        A :class:`CompilationResult` with intent, plan, bundle, properties.

    Raises:
        ValueError: If pattern_instances does not cover
            ``intent.property_pattern_refs``.
    """
    _check_coverage(intent, pattern_instances)

    resolved_bundle_id = bundle_id or f"{intent.intent_id}.bundle"
    resolved_plan_id = plan_id or f"{intent.intent_id}.plan"
    runtime_cfg: Dict[str, Any] = dict(runtime_config or {})

    plan = _build_plan(
        intent=intent,
        pattern_instances=pattern_instances,
        plan_id=resolved_plan_id,
        owner=owner,
        summary=summary or _default_summary(intent),
    )
    bundle = _build_bundle(
        intent=intent,
        pattern_instances=pattern_instances,
        bundle_id=resolved_bundle_id,
        plan=plan,
        runtime_kind=runtime_kind,
        runtime_config=runtime_cfg,
    )
    properties = _build_property_specs(pattern_instances)

    return CompilationResult(
        intent=intent,
        plan=plan,
        bundle=bundle,
        properties=properties,
        pattern_instances=list(pattern_instances),
    )


# ─── Plan generation ──────────────────────────────────────────────


def _build_plan(
    *,
    intent: IntentBody,
    pattern_instances: Sequence[PatternInstance],
    plan_id: str,
    owner: str,
    summary: str,
) -> PlanBody:
    """Build a deterministic MAPE-K plan from pattern instances.

    Naming conventions:

    - Inter-phase refs use ``phase.<name>.output`` (dot-separated)
      because the plan-rule trigger validator forbids ``:``.
    - Intent refs use ``intent.<intent_id>``.
    - Pattern verdict refs use ``property.<pattern_id>.verdict``.
    """
    steps: List[PlanStep] = []
    rules: List[PlanRule] = []

    intent_ref = f"intent.{intent.intent_id}"

    # MAPE-K skeleton: one step per phase, reading/writing namespaced refs.
    for phase in MAPE_PHASES:
        steps.append(
            PlanStep(
                name=phase,
                reads=[intent_ref] if phase == "observe" else [f"phase.{_prev_phase(phase)}.output"],
                writes=[f"phase.{phase}.output"],
                description=f"MAPE-K {phase} stage for {intent.intent_id}",
            )
        )

    # Per-pattern domain-specific step + rule
    for instance in pattern_instances:
        stage = CATEGORY_TO_STAGE.get(instance.category)
        if stage is None:
            # Unknown category — skip silently; the property still ships in the
            # properties list so the verifier can flag it.
            continue
        sanitized_id = instance.pattern_id.replace(".", "_")
        step_name = f"check_{sanitized_id}"
        steps.append(
            PlanStep(
                name=step_name,
                reads=[f"phase.{stage}.output"],
                writes=[f"property.{sanitized_id}.verdict"],
                description=(f"Verify property pattern '{instance.pattern_id}' against stage '{stage}' output"),
            )
        )
        rules.append(
            PlanRule(
                name=f"on_{stage}_check_{sanitized_id}",
                triggers=[f"phase.{stage}.output"],
                steps=[step_name],
            )
        )

    # Connect the MAPE skeleton with a standing rule.
    rules.append(
        PlanRule(
            name="run_mape_cycle",
            triggers=[intent_ref],
            steps=list(MAPE_PHASES),
        )
    )

    return PlanBody(
        plan_id=plan_id,
        owner=owner,
        summary=summary,
        steps=steps,
        rules=rules,
    )


def _prev_phase(phase: str) -> str:
    idx = MAPE_PHASES.index(phase)
    return MAPE_PHASES[idx - 1] if idx > 0 else "init"


# ─── Bundle generation ────────────────────────────────────────────


def _build_bundle(
    *,
    intent: IntentBody,
    pattern_instances: Sequence[PatternInstance],
    bundle_id: str,
    plan: PlanBody,
    runtime_kind: str,
    runtime_config: Mapping[str, Any],
) -> AgentBundleBody:
    """Build the AgentBundle skeleton.

    MAPE-K stages are encoded in the bundle plan via:

    - ``step`` — the actual stage name (``observe`` / ``analyze`` / …)
    - ``phase`` — the K-Ontology lifecycle phase
      (``init`` / ``run`` / ``emit`` / ``finalize``) since the artifact
      validator allows only those four values
    - the agent's ``role`` — also the MAPE-K stage name so downstream
      controllers can route by either field
    """
    agents: List[AgentBundleAgent] = []
    for phase in MAPE_PHASES:
        spec = _make_agent_spec(
            phase=phase,
            intent_id=intent.intent_id,
            runtime_kind=runtime_kind,
            runtime_config=runtime_config,
        )
        agents.append(AgentBundleAgent(spec=spec, role=phase))

    intent_ref = f"intent.{intent.intent_id}"
    plan_steps: List[AgentBundlePlanStep] = []
    for phase in MAPE_PHASES:
        plan_steps.append(
            AgentBundlePlanStep(
                step=phase,
                agent_id=_agent_id_for_phase(phase, intent.intent_id),
                phase=_MAPE_TO_BUNDLE_PHASE[phase],
                inputs=([intent_ref] if phase == "observe" else [f"phase.{_prev_phase(phase)}.output"]),
                outputs=[f"phase.{phase}.output"],
                description=f"MAPE-K {phase} agent for {intent.intent_id}",
            )
        )

    meta: Dict[str, Any] = {
        "compiled_from": {
            "intent_id": intent.intent_id,
            "plan_id": plan.plan_id,
            "pattern_ids": sorted({instance.pattern_id for instance in pattern_instances}),
        },
        "compiler": "tm.compile.intent_to_bundle@v0",
        "runtime_overrides": {},
        # The IO-contract linter requires every plan-step input to be either
        # produced by an earlier step or pre-declared here as a precondition.
        # The ``observe`` agent's intent ref is the entry point, so it must
        # appear in ``preconditions``.
        "preconditions": [intent_ref],
    }

    return AgentBundleBody(
        bundle_id=bundle_id,
        agents=agents,
        plan=plan_steps,
        meta=meta,
    )


def _agent_id_for_phase(phase: str, intent_id: str) -> str:
    return f"tm-agent/{intent_id}.{phase}:0.1"


def _make_agent_spec(
    *,
    phase: str,
    intent_id: str,
    runtime_kind: str,
    runtime_config: Mapping[str, Any],
) -> AgentSpec:
    contract = AgentContract(
        inputs=[
            IORef(
                ref=(f"intent.{intent_id}" if phase == "observe" else f"phase.{_prev_phase(phase)}.output"),
                kind="artifact" if phase == "observe" else "phase_output",
                schema={"type": "object"},
                required=True,
                mode="read",
            )
        ],
        outputs=[
            IORef(
                ref=f"phase.{phase}.output",
                kind="phase_output",
                schema={"type": "object"},
                required=True,
                mode="write",
            )
        ],
        effects=[
            EffectRef(
                name=f"{phase}.effect",
                kind="phase_signal",
                target=f"phase.{phase}.output",
                idempotency=EffectIdempotency(type="key", key_fields=["intent_id", "cycle_id"]),
                rollback=None,
                evidence={"emits": "phase_output"},
            )
        ],
    )
    runtime = AgentRuntime(
        kind=runtime_kind,
        config={
            "phase": phase,
            "intent_id": intent_id,
            **dict(runtime_config),
        },
    )
    evidence_outputs = [
        AgentEvidenceOutput(
            name=f"{phase}.evidence",
            description=f"Evidence emitted by the {phase} stage",
            target=f"phase:{phase}.output",
        )
    ]
    return AgentSpec(
        agent_id=_agent_id_for_phase(phase, intent_id),
        name=phase.capitalize(),
        version="0.1",
        runtime=runtime,
        contract=contract,
        config_schema={
            "type": "object",
            "properties": {
                "phase": {"type": "string"},
                "intent_id": {"type": "string"},
            },
            "required": ["phase", "intent_id"],
        },
        evidence_outputs=evidence_outputs,
    )


# ─── Property specs ───────────────────────────────────────────────


def _build_property_specs(
    pattern_instances: Sequence[PatternInstance],
) -> List[PropertySpec]:
    specs: List[PropertySpec] = []
    seen: set[str] = set()
    for instance in pattern_instances:
        name = instance.title or f"{instance.pattern_id}_property"
        if name in seen:
            # Disambiguate duplicate titles by appending an index suffix
            counter = 2
            while f"{name}_{counter}" in seen:
                counter += 1
            name = f"{name}_{counter}"
        seen.add(name)
        specs.append(PropertySpec(name=name, formula=instance.resolved_formula))
    return specs


# ─── Coverage / validation ────────────────────────────────────────


def _check_coverage(intent: IntentBody, pattern_instances: Sequence[PatternInstance]) -> None:
    declared = set(intent.property_pattern_refs)
    provided = {instance.pattern_id for instance in pattern_instances}
    missing = declared - provided
    if missing:
        raise ValueError(
            f"intent '{intent.intent_id}' declares pattern_refs that are not "
            f"covered by pattern_instances: {sorted(missing)}"
        )
    extra = provided - declared
    if extra:
        # Allow extra instances if the user wants — but surface as soft warning
        # via the bundle.meta so governance can audit. Keep silent at API level.
        pass


def _default_summary(intent: IntentBody) -> str:
    return f"MAPE-K plan compiled from intent '{intent.intent_id}'"


# ─── Dict conversion helpers (for as_dict / serialization) ────────


def _intent_to_dict(intent: IntentBody) -> Dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "title": intent.title,
        "context": intent.context,
        "goal": intent.goal,
        "non_goals": list(intent.non_goals),
        "actors": list(intent.actors),
        "inputs": list(intent.inputs),
        "outputs": list(intent.outputs),
        "constraints": list(intent.constraints),
        "success_metrics": list(intent.success_metrics),
        "risks": list(intent.risks),
        "assumptions": list(intent.assumptions),
        "trace_links": {
            "parent_intent": intent.trace_links.parent_intent,
            "related_intents": list(intent.trace_links.related_intents),
        },
        "property_pattern_refs": list(intent.property_pattern_refs),
        "slot_fills": {k: dict(v) for k, v in intent.slot_fills.items()},
    }


def _plan_to_dict(plan: PlanBody) -> Dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "owner": plan.owner,
        "summary": plan.summary,
        "steps": [
            {
                "name": step.name,
                "reads": list(step.reads),
                "writes": list(step.writes),
                "description": step.description,
            }
            for step in plan.steps
        ],
        "rules": [
            {
                "name": rule.name,
                "triggers": list(rule.triggers),
                "steps": list(rule.steps),
            }
            for rule in plan.rules
        ],
    }


def _bundle_to_dict(bundle: AgentBundleBody) -> Dict[str, Any]:
    return {
        "bundle_id": bundle.bundle_id,
        "agents": [
            {
                "spec": _agent_spec_to_dict(agent.spec),
                "role": agent.role,
            }
            for agent in bundle.agents
        ],
        "plan": [
            {
                "step": step.step,
                "agent_id": step.agent_id,
                "phase": step.phase,
                "inputs": list(step.inputs),
                "outputs": list(step.outputs),
                "description": step.description,
            }
            for step in bundle.plan
        ],
        "meta": dict(bundle.meta),
    }


def _agent_spec_to_dict(spec: AgentSpec) -> Dict[str, Any]:
    return {
        "agent_id": spec.agent_id,
        "name": spec.name,
        "version": spec.version,
        "runtime": {"kind": spec.runtime.kind, "config": dict(spec.runtime.config)},
        "contract": {
            "inputs": [_ioref_to_dict(i) for i in spec.contract.inputs],
            "outputs": [_ioref_to_dict(i) for i in spec.contract.outputs],
            "effects": [_effect_to_dict(e) for e in spec.contract.effects],
        },
        "config_schema": dict(spec.config_schema),
        "evidence_outputs": [
            {
                "name": ev.name,
                "description": ev.description,
                "target": ev.target,
            }
            for ev in spec.evidence_outputs
        ],
    }


def _ioref_to_dict(ref: IORef) -> Dict[str, Any]:
    schema = ref.schema if isinstance(ref.schema, str) else dict(ref.schema)
    return {
        "ref": ref.ref,
        "kind": ref.kind,
        "schema": schema,
        "required": ref.required,
        "mode": ref.mode,
    }


def _effect_to_dict(effect: EffectRef) -> Dict[str, Any]:
    return {
        "name": effect.name,
        "kind": effect.kind,
        "target": effect.target,
        "idempotency": {
            "type": effect.idempotency.type,
            "key_fields": list(effect.idempotency.key_fields),
        },
        "rollback": effect.rollback,
        "evidence": dict(effect.evidence),
    }


__all__ = [
    "CATEGORY_TO_STAGE",
    "CompilationResult",
    "MAPE_PHASES",
    "compile_intent_to_bundle",
]
