"""Abstraction-fidelity lint — reconcile ``meta.verify`` against the executable plan.

ISSUE-FORMLANG P0. See ``.plan/formal-language-completeness-review.md`` §3.

## Why this exists

An AgentBundle carries two descriptions of the same control logic:

- the **executable** side — ``bundle.plan`` steps with ``inputs`` / ``outputs``
  (what the controller actually wires and runs), and
- the **verification model** — ``meta.verify.steps`` with ``reads`` / ``writes``
  (the monotone-boolean abstraction that ``tm verify`` model-checks).

``tm.verify.bundle_adapter`` builds the Kripke structure from ``meta.verify``
alone, with ``fn=_noop`` — it never executes the real step code. Nothing
otherwise guarantees that the hand-written ``meta.verify`` abstraction agrees
with the executable plan. If they drift, ``tm verify`` returns a sound verdict
about a model that no longer describes the system — i.e. it verifies the wrong
thing. This is the only gap that can make verification *silently* misleading,
so it ranks above every CTL/state-model expressiveness gap.

This lint closes that gap deterministically (zero model checking, CI-friendly)
by reconciling the two descriptions step-by-step.

## Reconciliation contract

For a bundle that declares ``meta.verify``:

| Code | Severity | Meaning |
|------|----------|---------|
| ``VERIFY_FIDELITY_WAIVED`` | warning | author opted out via ``meta.verify.fidelity: manual`` — reconciliation skipped but the waiver is surfaced (accountable, not silent) |
| ``VERIFY_META_MALFORMED`` | error | ``meta.verify.steps`` missing/not a mapping — cannot reconcile |
| ``VERIFY_NO_PLAN`` | warning | ``meta.verify`` present but no plan steps to reconcile against |
| ``VERIFY_STEP_UNMODELED`` | error | a plan step has no ``meta.verify`` step — the executable step is invisible to verification (silent confidence gap) |
| ``VERIFY_STEP_ORPHAN`` | error | a ``meta.verify`` step has no plan step — the model verifies behavior the plan does not contain |
| ``VERIFY_WRITES_DRIFT`` | error | for a matched step, ``writes`` != plan ``outputs`` (fact production modeled wrongly) |
| ``VERIFY_READS_DRIFT`` | warning | for a matched step, ``reads`` != plan ``inputs`` (guard abstraction differs; advisory) |
| ``VERIFY_TRIGGER_UNREACHABLE`` | warning | a rule trigger fact is never in ``changed_paths``/``initial_store`` nor produced by any step — the rule (and its steps) can never fire, so properties over them are vacuous |

``writes`` drift is an error because it is the dangerous direction: a fact
modeled-but-not-produced makes liveness pass vacuously, and a fact
produced-but-not-modeled makes safety pass on an incomplete model. ``reads``
are existence guards — a faithful abstraction may legitimately drop or
strengthen them, so a difference is advisory.

The name-correspondence assumption (``meta.verify`` fact names == plan
input/output refs) matches the convention every shipped fixture follows. A
bundle that intentionally abstracts with different names can opt out with
``meta.verify.fidelity: manual`` — this records an accountable waiver rather
than silently passing, mirroring the design-loop "resolved / waived / dynamic"
discipline.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from tm.artifacts.models import AgentBundleBody
from tm.lint.plan_lint import LintIssue


def _as_str_set(value: Any) -> Set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    return {str(item) for item in value}


def _plan_io(bundle: AgentBundleBody) -> Dict[str, Tuple[Set[str], Set[str]]]:
    """Map plan step name -> (inputs, outputs), unioning duplicate names."""
    result: Dict[str, Tuple[Set[str], Set[str]]] = {}
    for step in bundle.plan:
        name = str(step.step)
        ins = {str(x) for x in (step.inputs or [])}
        outs = {str(x) for x in (step.outputs or [])}
        if name in result:
            prev_in, prev_out = result[name]
            result[name] = (prev_in | ins, prev_out | outs)
        else:
            result[name] = (ins, outs)
    return result


def _verify_steps(
    meta_verify: Mapping[str, Any],
) -> Tuple[Dict[str, Tuple[Set[str], Set[str], Set[str]]] | None, List[LintIssue]]:
    """Map verify step name -> (reads, writes, clears). Returns (None, [issue]) if malformed."""
    steps_raw = meta_verify.get("steps")
    if not isinstance(steps_raw, Mapping):
        return None, [
            LintIssue(
                code="VERIFY_META_MALFORMED",
                message="meta.verify.steps must be a mapping",
                severity="error",
                path="meta.verify.steps",
            )
        ]
    steps: Dict[str, Tuple[Set[str], Set[str], Set[str]]] = {}
    for name, raw in steps_raw.items():
        if not isinstance(raw, Mapping):
            return None, [
                LintIssue(
                    code="VERIFY_META_MALFORMED",
                    message=f"meta.verify.steps['{name}'] must be a mapping",
                    severity="error",
                    path=f"meta.verify.steps.{name}",
                )
            ]
        steps[str(name)] = (
            _as_str_set(raw.get("reads")),
            _as_str_set(raw.get("writes")),
            _as_str_set(raw.get("clears")),
        )
    return steps, []


def _reconcile_step_sets(
    plan_io: Dict[str, Tuple[Set[str], Set[str]]],
    verify_steps: Dict[str, Tuple[Set[str], Set[str], Set[str]]],
    issues: List[LintIssue],
) -> None:
    for name in sorted(set(plan_io) - set(verify_steps)):
        issues.append(
            LintIssue(
                code="VERIFY_STEP_UNMODELED",
                message=(
                    f"plan step '{name}' has no meta.verify.steps entry — "
                    "it is invisible to verification"
                ),
                severity="error",
                path=f"meta.verify.steps.{name}",
            )
        )
    for name in sorted(set(verify_steps) - set(plan_io)):
        issues.append(
            LintIssue(
                code="VERIFY_STEP_ORPHAN",
                message=(
                    f"meta.verify step '{name}' has no matching plan step — "
                    "the model verifies behavior the plan does not contain"
                ),
                severity="error",
                path=f"meta.verify.steps.{name}",
            )
        )


def _reconcile_step_io(
    plan_io: Dict[str, Tuple[Set[str], Set[str]]],
    verify_steps: Dict[str, Tuple[Set[str], Set[str], Set[str]]],
    issues: List[LintIssue],
) -> None:
    for name in sorted(set(plan_io) & set(verify_steps)):
        plan_in, plan_out = plan_io[name]
        v_reads, v_writes, v_clears = verify_steps[name]
        # A cleared fact is also an output the step touches, so reconcile the
        # union of writes and clears against the plan's declared outputs.
        v_effects = v_writes | v_clears
        if v_effects != plan_out:
            missing = sorted(plan_out - v_effects)
            extra = sorted(v_effects - plan_out)
            issues.append(
                LintIssue(
                    code="VERIFY_WRITES_DRIFT",
                    message=(
                        f"step '{name}' writes/clears drift vs plan outputs "
                        f"(unmodeled outputs: {missing}; phantom effects: {extra})"
                    ),
                    severity="error",
                    path=f"meta.verify.steps.{name}.writes",
                )
            )
        if v_reads != plan_in:
            missing = sorted(plan_in - v_reads)
            extra = sorted(v_reads - plan_in)
            issues.append(
                LintIssue(
                    code="VERIFY_READS_DRIFT",
                    message=(
                        f"step '{name}' reads differ from plan inputs "
                        f"(plan-only: {missing}; verify-only: {extra})"
                    ),
                    severity="warning",
                    path=f"meta.verify.steps.{name}.reads",
                )
            )


def _check_trigger_reachability(
    meta_verify: Mapping[str, Any],
    verify_steps: Dict[str, Tuple[Set[str], Set[str], Set[str]]],
    issues: List[LintIssue],
) -> None:
    """Warn on rule triggers that no step/seed can ever produce (vacuous rules)."""
    rules_raw = meta_verify.get("rules")
    if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, (str, bytes, bytearray)):
        return
    producible: Set[str] = set()
    initial_store = meta_verify.get("initial_store")
    if isinstance(initial_store, Mapping):
        producible |= {str(k) for k in initial_store.keys()}
    producible |= _as_str_set(meta_verify.get("changed_paths"))
    producible |= _as_str_set(meta_verify.get("initial_pending"))
    # Clears remove facts, so they do not "produce" trigger facts.
    for _reads, writes, _clears in verify_steps.values():
        producible |= writes
    for ridx, rule in enumerate(rules_raw):
        if not isinstance(rule, Mapping):
            continue
        rule_name = str(rule.get("name") or f"rule[{ridx}]")
        for trigger in _as_str_set(rule.get("triggers")):
            if trigger not in producible:
                issues.append(
                    LintIssue(
                        code="VERIFY_TRIGGER_UNREACHABLE",
                        message=(
                            f"rule '{rule_name}' trigger '{trigger}' is never in "
                            "changed_paths/initial_store nor written by any step — "
                            "the rule can never fire"
                        ),
                        severity="warning",
                        path=f"meta.verify.rules[{ridx}].triggers",
                    )
                )


def lint_verify_meta_fidelity(bundle: AgentBundleBody, raw_body: Mapping[str, Any]) -> List[LintIssue]:
    """Reconcile ``meta.verify`` against ``bundle.plan``.

    No-op (returns ``[]``) for bundles that do not declare ``meta.verify`` —
    the verification model is optional, and a bundle without one has nothing to
    reconcile.
    """
    meta = bundle.meta if isinstance(bundle.meta, Mapping) else {}
    meta_verify = meta.get("verify")
    if not isinstance(meta_verify, Mapping):
        return []

    fidelity_mode = str(meta_verify.get("fidelity") or "").strip().lower()
    if fidelity_mode == "manual":
        return [
            LintIssue(
                code="VERIFY_FIDELITY_WAIVED",
                message=(
                    "meta.verify.fidelity is 'manual' — abstraction fidelity "
                    "reconciliation skipped (accountable waiver)"
                ),
                severity="warning",
                path="meta.verify.fidelity",
            )
        ]

    verify_steps, malformed = _verify_steps(meta_verify)
    if verify_steps is None:
        return malformed

    issues: List[LintIssue] = []
    plan_io = _plan_io(bundle)
    if not plan_io:
        issues.append(
            LintIssue(
                code="VERIFY_NO_PLAN",
                message="meta.verify declared but bundle has no plan steps to reconcile against",
                severity="warning",
                path="plan",
            )
        )
    else:
        _reconcile_step_sets(plan_io, verify_steps, issues)
        _reconcile_step_io(plan_io, verify_steps, issues)

    _check_trigger_reachability(meta_verify, verify_steps, issues)
    return issues


__all__ = ["lint_verify_meta_fidelity"]
