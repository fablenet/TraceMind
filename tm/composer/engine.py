from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from tm.policy import PolicyEvaluator


@dataclass(frozen=True)
class CandidateRejection:
    template_id: str
    template_name: str
    code: str
    message: str
    evidence: Mapping[str, Any]
    signature: str | None


@dataclass
class Candidate:
    template_id: str
    template_name: str
    workflow_policy: Mapping[str, Any]
    candidate_id: str
    signature: str
    metrics: Mapping[str, Any]
    normalized: Mapping[str, float]
    guard_names: Sequence[str]


CONSERVATIVE_WEIGHTS = {
    "w_s": 5.0,
    "w_r": 4.0,
    "w_n": 3.0,
    "w_c": 2.0,
    "w_g": 0.5,
    "w_cov": 6.0,
}

AGGRESSIVE_WEIGHTS = {
    "v_t": 5.0,
    "v_s": 2.0,
    "v_r": 3.0,
    "v_n": 1.5,
    "v_cov": 8.0,
    "v_g": -0.5,
}

MODE_WEIGHTS = {
    "conservative": CONSERVATIVE_WEIGHTS,
    "aggressive": AGGRESSIVE_WEIGHTS,
}

TEMPLATES: list[Mapping[str, Any]] = [
    {
        "id": "T1",
        "name": "classify-validate-external-write",
        "steps": [
            {"capability": "compute.process", "description": "Classify input"},
            {"capability": "validate.result", "description": "Validate classification"},
            {
                "capability": "external.write",
                "description": "Write result to external system",
                "requires_guard": True,
            },
        ],
    },
    {
        "id": "T2",
        "name": "classify-case-audit",
        "steps": [
            {"capability": "compute.process", "description": "Classify input"},
            {"capability": "case.create", "description": "Create case record"},
            {"capability": "audit.record", "description": "Audit the operation"},
        ],
    },
    {
        "id": "T3",
        "name": "classify-guarded-write-case-audit",
        "steps": [
            {"capability": "compute.process", "description": "Classify input"},
            {
                "capability": "external.write",
                "description": "Guarded external write",
                "requires_guard": True,
            },
            {"capability": "case.create", "description": "Create case document"},
            {"capability": "audit.record", "description": "Audit the flow"},
        ],
    },
    {
        "id": "T4",
        "name": "classify-case-guarded-notify-audit",
        "steps": [
            {"capability": "compute.process", "description": "Classify input"},
            {"capability": "case.create", "description": "Create case document"},
            {
                "capability": "external.notify",
                "description": "Notify downstream system",
                "requires_guard": True,
            },
            {"capability": "audit.record", "description": "Audit the notification"},
        ],
    },
]


def _compute_signature(step_ids: Sequence[str], guard_names: Sequence[str]) -> str:
    segments = []
    for idx, step_id in enumerate(step_ids):
        guard_segment = f"[{guard_names[idx]}]" if idx < len(guard_names) and guard_names[idx] else ""
        segments.append(f"{step_id}{guard_segment}")
    payload = "->".join(segments)
    return payload


def _sanitize_identifier(value: str | None, default: str) -> str:
    if not value:
        return default
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower())
    cleaned = cleaned.strip("-")
    return cleaned or default


class WorkflowComposer:
    def __init__(
        self,
        intent: Mapping[str, Any],
        policy: Mapping[str, Any],
        capabilities: Sequence[Mapping[str, Any]],
        intent_ref: str,
        policy_ref: str,
        catalog_ref: str,
    ):
        self.intent = intent
        self.policy = policy
        self.intent_ref = intent_ref
        self.policy_ref = policy_ref
        self.catalog_ref = catalog_ref
        self.capabilities = {str(spec["capability_id"]): spec for spec in capabilities if "capability_id" in spec}
        self.policy_engine = PolicyEvaluator(policy)
        self.guards = list(policy.get("guards") or [])
        self.guard_lookup: dict[str, list[Mapping[str, Any]]] = {}
        for guard in self.guards:
            required_for = guard.get("required_for")
            if isinstance(required_for, str):
                self.guard_lookup.setdefault(required_for, []).append(guard)
            elif isinstance(required_for, Sequence):
                for entry in required_for:
                    self.guard_lookup.setdefault(str(entry), []).append(guard)

    def compose(self, modes: list[str], top_k: int = 1) -> Mapping[str, Any]:
        accepted: list[Mapping[str, Any]] = []
        rejections: list[CandidateRejection] = []
        candidates: list[Candidate] = []
        for template in TEMPLATES:
            candidate, rejection = self._build_candidate(template)
            if rejection:
                rejections.append(rejection)
            elif candidate:
                candidates.append(candidate)

        for mode in modes:
            weights = MODE_WEIGHTS.get(mode)
            if not weights:
                continue
            scored = [(mode, candidate, self._score_candidate(candidate, mode)) for candidate in candidates]
            scored.sort(key=lambda item: (item[2]["total_cost"], candidate_sort_key(item[1])))
            for _, candidate, score in scored[:top_k]:
                accepted.append(self._format_accepted_entry(candidate, mode, score))

        explanation = {
            "compose_result": {
                "intent_ref": self.intent_ref,
                "policy_ref": self.policy_ref,
                "catalog_ref": self.catalog_ref,
                "modes": modes,
                "accepted": accepted,
                "rejected": [
                    {
                        "candidate_id": rejection.signature or rejection.template_id,
                        "template": rejection.template_id,
                        "rejection": {
                            "code": rejection.code,
                            "message": rejection.message,
                            "evidence": rejection.evidence,
                        },
                    }
                    for rejection in sorted(
                        rejections,
                        key=lambda entry: (entry.code, entry.signature or entry.template_id),
                    )
                ],
            }
        }
        workflow_policy = accepted[0]["workflow_policy"] if accepted else {}
        result = {
            "workflow_policy": workflow_policy,
            "explanation": explanation,
        }
        return result

    def _build_candidate(self, template: Mapping[str, Any]) -> tuple[Candidate | None, CandidateRejection | None]:
        steps = []
        guard_names = []
        guard_entries: dict[str, Mapping[str, Any]] = {}
        state: dict[str, Any] = {}
        state_history: list[dict[str, Any]] = []
        for idx, step_def in enumerate(template["steps"]):
            cap_id = step_def["capability"]
            spec = self.capabilities.get(cap_id)
            if spec is None:
                return (
                    None,
                    CandidateRejection(
                        template_id=template["id"],
                        template_name=template["name"],
                        code="MISSING_CAPABILITY",
                        message=f"capability '{cap_id}' missing from catalog",
                        evidence={"capability_id": cap_id},
                        signature=None,
                    ),
                )
            guard = None
            if step_def.get("requires_guard") or spec.get("safety_contract", {}).get("side_effects"):
                guard_opts = self.guard_lookup.get(cap_id, [])
                guard_opts_sorted = sorted(guard_opts, key=lambda g: g.get("name") or "")
                if not guard_opts_sorted:
                    return (
                        None,
                        CandidateRejection(
                            template_id=template["id"],
                            template_name=template["name"],
                            code="GUARD_REQUIRED_BUT_MISSING",
                            message=f"guard required for capability '{cap_id}'",
                            evidence={"capability_id": cap_id},
                            signature=None,
                        ),
                    )
                guard = guard_opts_sorted[0]
                guard_entries[guard["name"]] = guard
            step_id = f"{template['id']}.step{idx + 1}"
            step_record: dict[str, Any] = {
                "step_id": step_id,
                "capability_id": cap_id,
                "description": step_def.get("description", ""),
            }
            if guard:
                step_record["guard"] = {
                    "name": guard["name"],
                    "type": guard.get("type"),
                    "required_for": guard.get("required_for"),
                    "scope": guard.get("scope"),
                }
            steps.append(step_record)
            guard_names.append(guard["name"] if guard else "")
            for extractor in spec.get("state_extractors") or []:
                produces = extractor.get("produces") or {}
                for key, value in produces.items():
                    state[key] = value.get("value", True) if isinstance(value, Mapping) else value
            state_history.append(dict(state))
        signature = _compute_signature([step["step_id"] for step in steps], guard_names)
        candidate_id = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8]
        policy_id = str(self.policy.get("policy_id") or "policy").lower()
        template_segment = _sanitize_identifier(template["id"], template["id"].lower())
        workflow_id = f"{policy_id}.{template_segment}.{candidate_id}"
        transitions = [{"from": steps[i]["step_id"], "to": steps[i + 1]["step_id"]} for i in range(len(steps) - 1)]
        guard_list = [dict(entry) for entry in guard_entries.values()]
        workflow = {
            "workflow_id": workflow_id,
            "intent_id": self.intent.get("intent_id"),
            "policy_id": self.policy.get("policy_id"),
            "name": f"{template['name']}",
            "version": self.intent.get("version"),
            "steps": steps,
            "transitions": transitions,
            "guards": guard_list,
            "explanation": {
                "intent_coverage": f"covers intent {self.intent.get('goal', {}).get('target')}",
                "capability_reasoning": ", ".join(step["capability_id"] for step in steps),
                "constraint_coverage": "validated against policy guards",
                "risks": [],
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"template": template["id"]},
        }
        final_state = state_history[-1] if state_history else {}
        invariant_result = self.policy_engine.check_state(final_state)
        if not invariant_result.succeeded:
            violation = invariant_result.violations[0]
            return (
                None,
                CandidateRejection(
                    template_id=template["id"],
                    template_name=template["name"],
                    code="POLICY_INVARIANT_VIOLATION",
                    message=violation.detail,
                    evidence={
                        "invariant_id": violation.rule_id,
                        "offending_step": steps[-1]["step_id"],
                        "offending_capability": steps[-1]["capability_id"],
                        "state_at_violation": final_state,
                    },
                    signature=signature,
                ),
            )
        goal_target = str(self.intent.get("goal", {}).get("target") or "")
        goal_covered = bool(goal_target and final_state.get(goal_target))
        if not goal_covered:
            return (
                None,
                CandidateRejection(
                    template_id=template["id"],
                    template_name=template["name"],
                    code="UNSATISFIABLE_INTENT",
                    message="workflow never produces intent goal",
                    evidence={"missing_goal": goal_target},
                    signature=signature,
                ),
            )
        metrics = dict(self._metrics_for_steps(steps))
        normalized = self._normalize_metrics(metrics)
        metrics["goal_coverage"] = 1
        metrics["policy_static_ok"] = 1
        candidate = Candidate(
            template_id=template["id"],
            template_name=template["name"],
            workflow_policy=workflow,
            candidate_id=candidate_id,
            signature=signature,
            metrics=metrics,
            normalized=normalized,
            guard_names=[name for name in guard_names if name],
        )
        return candidate, None

    def _metrics_for_steps(self, steps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        n_steps = len(steps)
        n_side_effect_steps = 0
        n_irreversible = 0
        n_nondet = 0
        n_guards = 0
        n_approval_guards = 0
        goal_coverage = 0
        policy_static_ok = 1
        for step in steps:
            cap_id = step["capability_id"]
            spec = self.capabilities[cap_id]
            safety = spec.get("safety_contract") or {}
            side_effects = safety.get("side_effects") or []
            if side_effects:
                n_side_effect_steps += 1
            rollback = safety.get("rollback") or {}

            if rollback.get("supported") is False:
                n_irreversible += 1
            determinism = safety.get("determinism")
            if determinism is False:
                n_nondet += 1
            if step.get("guard"):
                n_guards += 1
                guard_type = step["guard"].get("type")
                if guard_type == "approval":
                    n_approval_guards += 1
        return {
            "n_steps": n_steps,
            "n_side_effect_steps": n_side_effect_steps,
            "n_irreversible": n_irreversible,
            "n_nondet": n_nondet,
            "n_guards": n_guards,
            "n_approval_guards": n_approval_guards,
            "goal_coverage": goal_coverage,
            "policy_static_ok": policy_static_ok,
        }

    def _normalize_metrics(self, metrics: Mapping[str, Any]) -> Mapping[str, float]:
        N = metrics["n_steps"] or 1
        p_side_effect = metrics["n_side_effect_steps"] / N
        p_irreversible = metrics["n_irreversible"] / N
        p_nondet = metrics["n_nondet"] / N
        p_guards = min(1.0, metrics["n_guards"] / N)
        approval_denominator = max(1, metrics["n_side_effect_steps"])
        p_approval = min(1.0, metrics["n_approval_guards"] / approval_denominator)
        complexity = min(1.0, (N - 1) / (N + 3)) if N > 0 else 0.0
        return {
            "p_side_effect": p_side_effect,
            "p_irreversible": p_irreversible,
            "p_nondet": p_nondet,
            "p_guards": p_guards,
            "p_approval": p_approval,
            "complexity": complexity,
        }

    def _score_candidate(self, candidate: Candidate, mode: str) -> dict[str, Any]:
        normalized = candidate.normalized
        N_weights = MODE_WEIGHTS[mode]
        if mode == "conservative":
            cost_terms = {
                "side_effect": normalized["p_side_effect"] * N_weights["w_s"],
                "irreversible": normalized["p_irreversible"] * N_weights["w_r"],
                "nondet": normalized["p_nondet"] * N_weights["w_n"],
                "complexity": normalized["complexity"] * N_weights["w_c"],
                "guards": normalized["p_guards"] * N_weights["w_g"],
                "approval_coverage": (1 - normalized["p_approval"]) * N_weights["w_cov"],
            }
        else:
            cost_terms = {
                "complexity": normalized["complexity"] * N_weights["v_t"],
                "side_effect": normalized["p_side_effect"] * N_weights["v_s"],
                "irreversible": normalized["p_irreversible"] * N_weights["v_r"],
                "nondet": normalized["p_nondet"] * N_weights["v_n"],
                "guards": normalized["p_guards"] * N_weights["v_g"],
                "approval_coverage": (1 - normalized["p_approval"]) * N_weights["v_cov"],
            }
        total_cost = sum(cost_terms.values())
        score = {
            "mode": mode,
            "raw": candidate.metrics,
            "normalized": normalized,
            "weights": N_weights,
            "cost_terms": cost_terms,
            "total_cost": total_cost,
        }
        return score

    def _format_accepted_entry(self, candidate: Candidate, mode: str, score: dict[str, Any]) -> Mapping[str, Any]:
        invariants = self.policy.get("invariants") or []
        base_checks = {
            "invariants_passed": [
                inv.get("id") or inv.get("name") or inv.get("rule") or "unknown" for inv in invariants
            ],
            "guards_inserted": candidate.guard_names,
        }
        rationale = []
        metrics = candidate.metrics
        if metrics["n_side_effect_steps"] == 0:
            rationale.append("No side effects are triggered.")
        else:
            rationale.append(f"{metrics['n_side_effect_steps']} side-effect steps reported.")
        if metrics["n_irreversible"] == 0:
            rationale.append("All steps are rollback-friendly.")
        if metrics["n_nondet"] == 0:
            rationale.append("Workflow remains deterministic.")
        goal_target = self.intent.get("goal", {}).get("target")
        if goal_target:
            rationale.append(f"Satisfies goal: {goal_target}.")
        score_data = score.copy()
        score_data["weights"] = dict(score_data["weights"])
        return {
            "candidate_id": candidate.candidate_id,
            "mode": mode,
            "workflow_policy": candidate.workflow_policy,
            "signature": candidate.signature,
            "template": candidate.template_id,
            "score": score_data,
            "rationale": rationale,
            "checks": base_checks,
        }


def candidate_sort_key(candidate: Candidate) -> tuple[int, str]:
    steps = candidate.metrics["n_steps"]
    signature = candidate.signature
    return steps, signature
