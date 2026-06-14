"""Profile governance (Stage 7-0.9, plan §5b).

A domain 5W1H profile sets the *strength* of the design-time consistency gate
(7-0.8). Changing it must go through a ``ProposedChangePlanBody`` (candidate →
verify → human accept), and any **gate-weakening** (severity downgrade, dropped
required slot, required-slot enforcement retargeted onto an off dimension) must
be surfaced explicitly so a human approver consciously signs off — never a
silent on-disk edit.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    ProposedChangePlanBody,
    verify,
)
from tm.intent.profile_governance import (
    CHANGE_REQUIRED_SLOT,
    CHANGE_REQUIRED_SLOTS_DIMENSION,
    CHANGE_SEVERITY,
    PROFILE_OP_CREATE,
    PROFILE_OP_UPDATE,
    assess_profile_change,
    build_profile_change_proposal,
)


def _domain_profile(**overrides) -> dict:
    body = {
        "profile_id": "demo.domain.v1",
        "domain": "demo",
        "extends": "base",
    }
    body.update(overrides)
    return body


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _change_for(impact, kind, target):
    return next(c for c in impact.changes if c.kind == kind and c.target == target)


# ─── severity downgrade / upgrade ───────────────────────────────────


def test_severity_downgrade_is_a_weakening():
    impact = assess_profile_change(_domain_profile(severity_overrides={"who": "warn"}))
    assert impact.operation == PROFILE_OP_CREATE
    assert impact.weakens_gate is True
    change = _change_for(impact, CHANGE_SEVERITY, "who")
    assert (change.before, change.after, change.weakens) == ("error", "warn", True)


def test_severity_to_off_is_a_weakening():
    impact = assess_profile_change(_domain_profile(severity_overrides={"what": "off"}))
    assert impact.weakens_gate is True
    assert _change_for(impact, CHANGE_SEVERITY, "what").weakens is True


def test_severity_upgrade_is_not_a_weakening():
    impact = assess_profile_change(_domain_profile(severity_overrides={"when": "error"}))
    change = _change_for(impact, CHANGE_SEVERITY, "when")
    assert (change.before, change.after) == ("warn", "error")
    assert change.weakens is False
    assert impact.weakens_gate is False


# ─── required_slots ─────────────────────────────────────────────────


def test_dropping_required_slot_is_a_weakening(tmp_path: Path):
    baseline = _write(
        tmp_path,
        "baseline.yaml",
        _domain_profile(profile_id="demo.domain.v1", required_slots=["anon_token"]),
    )
    proposed = _domain_profile(profile_id="demo.domain.v1")  # no required_slots
    impact = assess_profile_change(proposed, base_dir=tmp_path, baseline=baseline)
    assert impact.operation == PROFILE_OP_UPDATE
    assert impact.weakens_gate is True
    assert _change_for(impact, CHANGE_REQUIRED_SLOT, "anon_token").weakens is True


def test_adding_required_slot_strengthens(tmp_path: Path):
    baseline = _write(tmp_path, "baseline.yaml", _domain_profile(profile_id="demo.domain.v1"))
    proposed = _domain_profile(profile_id="demo.domain.v1", required_slots=["anon_token"])
    impact = assess_profile_change(proposed, base_dir=tmp_path, baseline=baseline)
    assert impact.weakens_gate is False
    assert _change_for(impact, CHANGE_REQUIRED_SLOT, "anon_token").weakens is False


def test_required_slots_dimension_onto_off_is_a_weakening():
    impact = assess_profile_change(
        _domain_profile(
            severity_overrides={"where": "off"},
            required_slots_dimension="where",
        )
    )
    change = _change_for(impact, CHANGE_REQUIRED_SLOTS_DIMENSION, "where")
    assert change.weakens is True


# ─── proposal construction surfaces weakening ───────────────────────


def test_proposal_flags_weakening_in_policy_requirements():
    proposal = build_profile_change_proposal(
        effect_ref="tm/intent/profiles/demo.domain.v1.yaml",
        proposed_raw=_domain_profile(severity_overrides={"who": "off"}),
        summary="weaken who to off",
    )
    target_state = proposal["decisions"][0]["target_state"]
    assert target_state["operation"] == PROFILE_OP_CREATE
    assert target_state["impact"]["weakens_gate"] is True
    assert any("GATE-WEAKENING" in r for r in proposal["policy_requirements"])
    assert any("UNACKNOWLEDGED" in r for r in proposal["policy_requirements"])


def test_proposal_records_acknowledger():
    proposal = build_profile_change_proposal(
        effect_ref="tm/intent/profiles/demo.domain.v1.yaml",
        proposed_raw=_domain_profile(severity_overrides={"who": "off"}),
        summary="weaken who to off",
        acknowledged_by="alice@example",
    )
    target_state = proposal["decisions"][0]["target_state"]
    assert target_state["acknowledged_by"] == "alice@example"
    assert any("alice@example" in r for r in proposal["policy_requirements"])


def test_non_weakening_proposal_has_no_gate_warning():
    proposal = build_profile_change_proposal(
        effect_ref="tm/intent/profiles/demo.domain.v1.yaml",
        proposed_raw=_domain_profile(severity_overrides={"when": "error"}),
        summary="strengthen when",
    )
    assert not any("GATE-WEAKENING" in r for r in proposal["policy_requirements"])
    assert proposal["decisions"][0]["target_state"]["impact"]["weakens_gate"] is False


# ─── proposal goes through the artifact governance lifecycle ────────


def test_profile_proposal_verifies_to_accepted():
    body_raw = build_profile_change_proposal(
        effect_ref="tm/intent/profiles/demo.domain.v1.yaml",
        proposed_raw=_domain_profile(severity_overrides={"who": "off"}),
        summary="weaken who to off",
        acknowledged_by="alice@example",
    )
    body = ProposedChangePlanBody.from_mapping(body_raw)
    envelope = ArtifactEnvelope(
        artifact_id="proposal.profile.demo.domain.v1",
        status=ArtifactStatus.CANDIDATE,
        artifact_type=ArtifactType.PROPOSED_CHANGE_PLAN,
        version="v0",
        created_by="tester",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )
    candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)

    accepted, report = verify(candidate)

    assert report.errors == [], report.errors
    assert accepted is not None
    assert accepted.envelope.status == ArtifactStatus.ACCEPTED


# ─── determinism ────────────────────────────────────────────────────


def test_impact_is_deterministic():
    raw = _domain_profile(severity_overrides={"who": "off", "when": "error"})
    a = assess_profile_change(raw).to_dict()
    b = assess_profile_change(raw).to_dict()
    assert a == b
