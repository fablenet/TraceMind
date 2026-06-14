"""Profile governance — domain-profile changes go through ProposedChangePlan.

Stage 7-0.9 (plan §5b "Profile 治理"). A domain 5W1H profile's
``severity_overrides`` (especially downgrading ``error`` → ``warn`` / ``off``)
and its ``required_slots`` directly set the *strength* of the design-time
consistency gate (Task 7-0.8). Editing a profile silently on disk would quietly
weaken that gate. So a profile change must be staged as a
:class:`tm.artifacts.models.ProposedChangePlanBody` (candidate → ``verify`` →
human ``accept``), and the proposal must make any **gate-weakening** explicit so
a human approver consciously signs off on it.

This module is deterministic and zero-LLM (guarded by
``scripts/check_no_llm_in_completeness.py``): it computes a precise before/after
impact (which dimensions were downgraded, which required slots were dropped) and
builds the governance proposal body. It does **not** apply the change — applying
happens only after a human accepts the artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tm.intent.completeness import (
    ALL_DIMENSIONS,
    Dimension,
    Profile,
    Severity,
    load_profile,
    load_profile_from_mapping,
)

PROFILE_OP_CREATE = "create_profile"
PROFILE_OP_UPDATE = "update_profile"

#: Higher rank = stronger gate. A downgrade (after < before) weakens the gate.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.OFF: 0,
    Severity.WARN: 1,
    Severity.ERROR: 2,
}

CHANGE_SEVERITY = "severity"
CHANGE_REQUIRED_SLOT = "required_slot"
CHANGE_REQUIRED_SLOTS_DIMENSION = "required_slots_dimension"


@dataclass(frozen=True)
class ProfileChange:
    """One field-level change between the baseline and proposed profile."""

    kind: str
    target: str
    before: str | None
    after: str | None
    weakens: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "weakens": self.weakens,
            "note": self.note,
        }


@dataclass(frozen=True)
class ProfileChangeImpact:
    """Deterministic before/after impact of a proposed profile change."""

    profile_id: str
    operation: str
    baseline_profile: str
    changes: tuple[ProfileChange, ...]

    @property
    def weakenings(self) -> tuple[ProfileChange, ...]:
        return tuple(c for c in self.changes if c.weakens)

    @property
    def weakens_gate(self) -> bool:
        return any(c.weakens for c in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "operation": self.operation,
            "baseline_profile": self.baseline_profile,
            "weakens_gate": self.weakens_gate,
            "changes": [c.to_dict() for c in self.changes],
            "weakenings": [c.to_dict() for c in self.weakenings],
        }


def diff_profiles(baseline: Profile, proposed: Profile, *, operation: str) -> ProfileChangeImpact:
    """Compute the deterministic gate impact of ``baseline`` → ``proposed``.

    Weakenings (each surfaced with ``weakens=True``):
      * a dimension severity downgraded (``error`` → ``warn`` / ``off``, or
        ``warn`` → ``off``);
      * a ``required_slot`` removed;
      * ``required_slots_dimension`` retargeted onto a dimension whose proposed
        severity is ``off`` (which neuters required-slot enforcement).
    Strengthenings and neutral edits are still recorded (``weakens=False``).
    """
    changes: list[ProfileChange] = []

    for dim in ALL_DIMENSIONS:
        b = baseline.severity(dim)
        a = proposed.severity(dim)
        if a is b:
            continue
        weakens = _SEVERITY_RANK[a] < _SEVERITY_RANK[b]
        note = "downgrade weakens gate" if weakens else "upgrade strengthens gate"
        changes.append(
            ProfileChange(
                kind=CHANGE_SEVERITY,
                target=dim.value,
                before=b.value,
                after=a.value,
                weakens=weakens,
                note=note,
            )
        )

    before_slots = set(baseline.required_slots)
    after_slots = set(proposed.required_slots)
    for slot in sorted(before_slots - after_slots):
        changes.append(
            ProfileChange(
                kind=CHANGE_REQUIRED_SLOT,
                target=slot,
                before="required",
                after=None,
                weakens=True,
                note="dropped required slot weakens gate",
            )
        )
    for slot in sorted(after_slots - before_slots):
        changes.append(
            ProfileChange(
                kind=CHANGE_REQUIRED_SLOT,
                target=slot,
                before=None,
                after="required",
                weakens=False,
                note="added required slot strengthens gate",
            )
        )

    if baseline.required_slots_dimension is not proposed.required_slots_dimension:
        target_off = proposed.severity(proposed.required_slots_dimension) is Severity.OFF
        changes.append(
            ProfileChange(
                kind=CHANGE_REQUIRED_SLOTS_DIMENSION,
                target=proposed.required_slots_dimension.value,
                before=baseline.required_slots_dimension.value,
                after=proposed.required_slots_dimension.value,
                weakens=target_off,
                note=(
                    "retargeted onto an off dimension; required-slot enforcement disabled"
                    if target_off
                    else "retargeted required-slot enforcement"
                ),
            )
        )

    return ProfileChangeImpact(
        profile_id=proposed.profile_id,
        operation=operation,
        baseline_profile=baseline.profile_id,
        changes=tuple(changes),
    )


def assess_profile_change(
    proposed_raw: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
    baseline: str | Path | None = None,
) -> ProfileChangeImpact:
    """Resolve the proposed profile body and diff it against its baseline.

    ``baseline`` is the existing profile being updated (``update_profile``); when
    omitted the change is a ``create_profile`` measured against the built-in
    ``base`` profile, so a brand-new profile that turns off a base-error
    dimension is still surfaced as a weakening relative to base.
    """
    proposed = load_profile_from_mapping(proposed_raw, base_dir=base_dir)
    if baseline is None:
        operation = PROFILE_OP_CREATE
        baseline_profile = load_profile("base", base_dir=base_dir)
    else:
        operation = PROFILE_OP_UPDATE
        baseline_profile = load_profile(baseline, base_dir=base_dir)
    return diff_profiles(baseline_profile, proposed, operation=operation)


def build_profile_change_proposal(
    *,
    effect_ref: str,
    proposed_raw: Mapping[str, Any],
    summary: str,
    base_dir: Path | None = None,
    baseline: str | Path | None = None,
    acknowledged_by: str | None = None,
    reasoning_trace: str = "Profile change submitted via governance",
) -> dict[str, Any]:
    """Build a ``ProposedChangePlanBody`` raw dict for a profile change.

    The proposal embeds the deterministic :class:`ProfileChangeImpact` in the
    decision ``target_state`` and, when the change weakens the gate, adds an
    explicit policy requirement that a human approver acknowledge it — so the
    weakening can never slip through silently. The returned dict is meant to be
    wrapped as a ``candidate`` artifact and run through ``tm.artifacts.verify``.
    """
    impact = assess_profile_change(proposed_raw, base_dir=base_dir, baseline=baseline)
    profile_id = impact.profile_id

    policy_requirements = [
        "Accepting this proposal materialises the profile; no silent on-disk edit.",
        "All existing profiles must continue to load and existing intents re-checked.",
    ]
    if impact.weakens_gate:
        dims = ", ".join(c.target for c in impact.weakenings)
        policy_requirements.insert(
            0,
            f"GATE-WEAKENING change ({dims}): a human approver MUST consciously "
            f"accept this; acknowledged_by={acknowledged_by or 'UNACKNOWLEDGED'}.",
        )

    target_state: dict[str, Any] = {
        "operation": impact.operation,
        "profile_id": profile_id,
        "body": dict(proposed_raw),
        "impact": impact.to_dict(),
    }
    if acknowledged_by:
        target_state["acknowledged_by"] = acknowledged_by

    return {
        "plan_id": f"profile.{impact.operation}.{profile_id}",
        "intent_id": "intent.profile_governance.curation",
        "summary": summary,
        "decisions": [
            {
                "effect_ref": effect_ref,
                "target_state": target_state,
                "idempotency_key": f"profile.{impact.operation}.{profile_id}.v1",
                "reasoning_trace": reasoning_trace,
            }
        ],
        "llm_metadata": {
            "model": "none",
            "prompt_hash": "n/a",
            "determinism_hint": "deterministic",
        },
        "policy_requirements": policy_requirements,
    }


__all__ = [
    "CHANGE_REQUIRED_SLOT",
    "CHANGE_REQUIRED_SLOTS_DIMENSION",
    "CHANGE_SEVERITY",
    "PROFILE_OP_CREATE",
    "PROFILE_OP_UPDATE",
    "ProfileChange",
    "ProfileChangeImpact",
    "assess_profile_change",
    "build_profile_change_proposal",
    "diff_profiles",
]
