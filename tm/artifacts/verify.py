from __future__ import annotations

import re
from typing import Any, Mapping, Sequence, Tuple

from .hash import body_hash
from .models import (
    AgentBundleBody,
    AgentNetworkBody,
    Artifact,
    ArtifactStatus,
    ArtifactType,
    EnvSnapshotBody,
    ExecutionReportBody,
    IntentSessionBody,
    PlanBody,
    PlanRule,
    PropertyPatternBody,
    ProposedChangePlanBody,
)
from .report import ArtifactVerificationReport
from .validator import (
    validate_agent_network_spec,
    validate_intent_session_spec,
    validate_property_pattern_spec,
)
from tm.lint.agent_network_lint import lint_agent_network
from tm.lint.io_contract_lint import lint_agent_bundle_io_contract, lint_plan_io_contract
from tm.lint.plan_lint import LintIssue

_SUPPORTED_VERSION_PREFIX = "v0"
_TRIGGER_PATTERN = re.compile(r"^[A-Za-z0-9_\.\[\]\*\$]+$")
_BUNDLE_PHASES = {"init", "run", "emit", "finalize"}


def _is_supported_version(version: str) -> bool:
    return version == _SUPPORTED_VERSION_PREFIX or version.startswith(f"{_SUPPORTED_VERSION_PREFIX}.")


def _validate_plan_steps(plan: PlanBody, raw_steps: Sequence[Any] | None, report: ArtifactVerificationReport) -> None:
    if raw_steps is None:
        report.add_error("plan body missing 'steps' definition")
        return
    if not isinstance(raw_steps, Sequence):
        report.add_error("plan.steps must be a sequence")
        return
    seen: set[str] = set()
    for idx, step in enumerate(plan.steps):
        path = f"steps[{idx}]"
        raw_step = raw_steps[idx] if idx < len(raw_steps) else {}
        if not isinstance(raw_step, Mapping):
            report.add_error(f"{path} must be a mapping")
            continue
        name = step.name
        if not name:
            report.add_error(f"{path}.name must be a non-empty string")
        else:
            if name in seen:
                report.add_error(f"{path}.name '{name}' is not unique")
            seen.add(name)
        if "reads" not in raw_step:
            report.add_error(f"{path} missing 'reads' field")
        elif not isinstance(step.reads, list):
            report.add_error(f"{path}.reads must be a list")
        if "writes" not in raw_step:
            report.add_error(f"{path} missing 'writes' field")
        elif not isinstance(step.writes, list):
            report.add_error(f"{path}.writes must be a list")


def _validate_rule(rule: PlanRule, step_names: Sequence[str], report: ArtifactVerificationReport) -> None:
    if not rule.triggers:
        report.add_error(f"rule '{rule.name}' must declare at least one trigger")
    for trigger in rule.triggers:
        if not isinstance(trigger, str) or not trigger.strip():
            report.add_error(f"rule '{rule.name}' trigger must be a non-empty string")
            continue
        if not _TRIGGER_PATTERN.match(trigger):
            report.add_error(f"rule '{rule.name}' trigger '{trigger}' contains invalid characters")
    if not rule.steps:
        report.add_error(f"rule '{rule.name}' must reference at least one step")
    for target in rule.steps:
        if target not in step_names:
            report.add_error(f"rule '{rule.name}' references undefined step '{target}'")


def _validate_plan_rules(plan: PlanBody, report: ArtifactVerificationReport) -> None:
    step_names = [step.name for step in plan.steps if step.name]
    for rule in plan.rules:
        _validate_rule(rule, step_names, report)


def _validate_plan_body(body: PlanBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport) -> None:
    raw_steps = raw_body.get("steps")
    _validate_plan_steps(body, raw_steps, report)
    raw_rules = raw_body.get("rules", [])
    if raw_rules is not None and not isinstance(raw_rules, Sequence):
        report.add_error("plan.rules must be a sequence if provided")
    _validate_plan_rules(body, report)
    lint_issues = lint_plan_io_contract(raw_body)
    _report_lint_issues(report, lint_issues)


def _report_lint_issues(report: ArtifactVerificationReport, issues: Sequence[LintIssue]) -> None:
    for issue in issues:
        suffix = f" (path: {issue.path})" if issue.path else ""
        report.add_error(f"{issue.code}: {issue.message}{suffix}")


def _validate_agent_bundle(
    body: AgentBundleBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    if not body.agents:
        report.add_error("agent bundle must declare at least one agent")
    agent_ids = {agent.spec.agent_id for agent in body.agents}
    raw_plan = raw_body.get("plan")
    if raw_plan is None:
        report.add_error("agent bundle missing 'plan'")
        return
    if not isinstance(raw_plan, Sequence) or isinstance(raw_plan, str):
        report.add_error("agent bundle plan must be a sequence")
        return
    for idx, step in enumerate(body.plan):
        path = f"plan[{idx}]"
        if not step.step:
            report.add_error(f"{path}.step must be a non-empty string")
        if not step.agent_id:
            report.add_error(f"{path}.agent_id must be a non-empty string")
        elif step.agent_id not in agent_ids:
            report.add_error(f"{path}.agent_id '{step.agent_id}' is not registered")
        if step.phase and step.phase not in _BUNDLE_PHASES:
            report.add_error(f"{path}.phase '{step.phase}' is not allowed")
        if not isinstance(step.inputs, list):
            report.add_error(f"{path}.inputs must be a list")
        if not isinstance(step.outputs, list):
            report.add_error(f"{path}.outputs must be a list")


def _validate_env_snapshot(
    body: EnvSnapshotBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    if not body.snapshot_id:
        report.add_error("env snapshot must declare 'snapshot_id'")
    if not body.timestamp:
        report.add_error("env snapshot must declare 'timestamp'")
    if not body.environment:
        report.add_error("env snapshot 'environment' must not be empty")
    if not body.data_hash:
        report.add_error("env snapshot must declare 'data_hash'")
    constraints_raw = raw_body.get("constraints")
    if constraints_raw is not None and not isinstance(constraints_raw, Sequence):
        report.add_error("env snapshot 'constraints' must be a sequence if provided")


def _validate_proposed_change_plan(
    body: ProposedChangePlanBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    if not body.plan_id:
        report.add_error("proposed plan must declare 'plan_id'")
    if not body.intent_id:
        report.add_error("proposed plan must declare 'intent_id'")
    if not body.decisions:
        report.add_error("proposed plan must include at least one decision")
    for idx, decision in enumerate(body.decisions):
        if not decision.effect_ref:
            report.add_error(f"decisions[{idx}].effect_ref must be non-empty")
        if not decision.idempotency_key:
            report.add_error(f"decisions[{idx}].idempotency_key must be non-empty")
        if not decision.target_state:
            report.add_error(f"decisions[{idx}].target_state must be provided")
    if not body.summary:
        report.add_error("proposed plan must include a 'summary'")
    policy_raw = raw_body.get("policy_requirements")
    if policy_raw is not None and not isinstance(policy_raw, Sequence):
        report.add_error("policy_requirements must be a sequence")


def _validate_property_pattern(
    body: PropertyPatternBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    """Lifecycle validation for PropertyPattern artifacts (Stage 5-3 task 3.4).

    Two layers:

    1. **Schema** — reuse the AST schema validator so a candidate body must
       satisfy ``validate_property_pattern_spec``. Catches malformed slots,
       missing required fields, invalid category, etc.
    2. **Template ↔ slot consistency** — every ``{slot}`` placeholder in
       ``formula_template`` must reference a declared slot; every required
       slot must appear in the template. This avoids "dead slots" (declared
       but not referenced) and "phantom slots" (referenced but undeclared).
    """
    try:
        validate_property_pattern_spec(raw_body)
    except Exception as exc:  # jsonschema.ValidationError, ValueError, etc.
        report.add_error(f"property pattern schema validation failed: {exc}")
        return

    import string

    formatter = string.Formatter()
    placeholders = {
        field_name for _literal, field_name, _format, _conv in formatter.parse(body.formula_template) if field_name
    }
    declared_slots = {slot.name for slot in body.slots}
    required_slots = {slot.name for slot in body.slots if slot.required}

    phantom = placeholders - declared_slots
    if phantom:
        report.add_error(f"property pattern formula_template references undeclared slot(s): {sorted(phantom)}")
    unused_required = required_slots - placeholders
    if unused_required:
        report.add_error(f"property pattern declares required slot(s) not used by template: {sorted(unused_required)}")


def _validate_execution_report(
    body: ExecutionReportBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    if not body.report_id:
        report.add_error("execution report must declare 'report_id'")
    if not body.execution_hash:
        report.add_error("execution report must declare 'execution_hash'")
    if not body.policy_decisions:
        report.add_error("execution report must include policy decisions")
    policy_raw = raw_body.get("policy_decisions")
    if policy_raw is not None and not isinstance(policy_raw, Sequence):
        report.add_error("execution report 'policy_decisions' must be a sequence")


def _validate_agent_network(
    body: AgentNetworkBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    """Lifecycle validation for AgentNetwork artifacts (Phase 6 Stage 6-1.6).

    Three layers, matching the governance contract documented in
    ``docs/specs/k-ontology-v0.3.md`` §5:

    1. **JSON schema** — reuse the AST schema validator so a candidate body
       must satisfy ``validate_agent_network_spec``. Catches malformed types,
       missing required fields, unknown enum values
    2. **Topology lint** — run ``lint_agent_network`` and surface every
       error-severity issue (warnings remain advisory but are not blocking).
       This covers leaf-to-leaf edges, leaf-patches-center, KPI name shape,
       tree-reserved topology, etc.
    3. **Body hash determinism** — same canonical hash as every other artifact
       body (handled in ``verify`` itself)
    """
    try:
        validate_agent_network_spec(raw_body)
    except Exception as exc:  # jsonschema / ValueError / TypeError
        report.add_error(f"agent network schema validation failed: {exc}")
        return

    issues = lint_agent_network(raw_body)
    for issue in issues:
        if issue.severity != "error":
            continue
        suffix = f" (path: {issue.path})" if issue.path else ""
        report.add_error(f"{issue.code}: {issue.message}{suffix}")


def _validate_intent_session(
    body: IntentSessionBody, raw_body: Mapping[str, Any], report: ArtifactVerificationReport
) -> None:
    """Lifecycle validation for IntentSession artifacts (Phase 7 Stage 7-2.1).

    Two layers (the design-loop transition gating is added in Stage 7-2.2):

    1. **JSON schema** — reuse ``validate_intent_session_spec`` so a candidate
       body must satisfy the K-Ontology v0.4 schema (status / current_step /
       turn action enums, required fields).
    2. **Journal + seal structure** — the append-only ``turns`` journal must
       carry strictly increasing ``seq`` values, and a ``sealed`` session MUST
       carry a ``sign_off`` (full uncertainty-closure enforcement lands in
       Stage 7-2.8).
    """
    try:
        validate_intent_session_spec(raw_body)
    except Exception as exc:  # jsonschema / ValueError / TypeError
        report.add_error(f"intent session schema validation failed: {exc}")
        return

    previous: int | None = None
    for idx, turn in enumerate(body.turns):
        if previous is not None and turn.seq <= previous:
            report.add_error(
                f"turns[{idx}].seq must be strictly increasing (got {turn.seq} after {previous})"
            )
        previous = turn.seq

    if body.status == "sealed" and body.sign_off is None:
        report.add_error("sealed intent session must carry a 'sign_off' record")


def _apply_success_metadata(artifact: Artifact, computed_hash: str) -> None:
    artifact.envelope.body_hash = computed_hash
    hashes = artifact.envelope.meta.get("hashes")
    if not isinstance(hashes, dict):
        hashes = {}
    hashes["body_hash"] = computed_hash
    artifact.envelope.meta["hashes"] = hashes
    artifact.envelope.meta["determinism"] = True
    artifact.envelope.meta["produced_by"] = f"tracemind.verifier.{_SUPPORTED_VERSION_PREFIX}"
    artifact.envelope.status = ArtifactStatus.ACCEPTED


def verify(candidate: Artifact) -> Tuple[Artifact | None, ArtifactVerificationReport]:
    report = ArtifactVerificationReport(artifact_id=candidate.envelope.artifact_id)
    if candidate.envelope.status != ArtifactStatus.CANDIDATE:
        report.add_error("artifact status must be 'candidate' for verification")
    if not _is_supported_version(candidate.envelope.version):
        report.add_error(f"unsupported artifact version '{candidate.envelope.version}'")
    if candidate.envelope.artifact_type == ArtifactType.PLAN and isinstance(candidate.body, PlanBody):
        _validate_plan_body(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.AGENT_BUNDLE and isinstance(candidate.body, AgentBundleBody):
        _validate_agent_bundle(candidate.body, candidate.body_raw, report)
        lint_issues = lint_agent_bundle_io_contract(candidate.body, candidate.body_raw)
        _report_lint_issues(report, lint_issues)
    if candidate.envelope.artifact_type == ArtifactType.ENVIRONMENT_SNAPSHOT and isinstance(
        candidate.body, EnvSnapshotBody
    ):
        _validate_env_snapshot(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.PROPOSED_CHANGE_PLAN and isinstance(
        candidate.body, ProposedChangePlanBody
    ):
        _validate_proposed_change_plan(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.EXECUTION_REPORT and isinstance(
        candidate.body, ExecutionReportBody
    ):
        _validate_execution_report(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.PROPERTY_PATTERN and isinstance(
        candidate.body, PropertyPatternBody
    ):
        _validate_property_pattern(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.AGENT_NETWORK and isinstance(candidate.body, AgentNetworkBody):
        _validate_agent_network(candidate.body, candidate.body_raw, report)
    if candidate.envelope.artifact_type == ArtifactType.INTENT_SESSION and isinstance(
        candidate.body, IntentSessionBody
    ):
        _validate_intent_session(candidate.body, candidate.body_raw, report)
    if report.errors:
        return None, report
    computed = body_hash(candidate.body_raw)
    _apply_success_metadata(candidate, computed)
    report.details["body_hash"] = computed
    report.mark_success()
    return candidate, report
