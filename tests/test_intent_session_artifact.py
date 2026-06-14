"""Tests for the IntentSession artifact kind (K-Ontology v0.4 / Stage 7-2.1).

Promotes the iterative-design journal to a first-class artifact kind. Covers:

- File-level JSON schema validation (round-trip against schemas/v0)
- AST-level in-code schema validation (``validate_intent_session_spec``)
- Dataclass parsing via ``from_mapping`` (enum guards, optional fields)
- ``verify`` lifecycle: clean working candidate ➜ accepted, body_hash stamped,
  idempotent re-verify, and every blocking structural rule
- Vocabulary drift guard: the body model's local frozensets stay identical to
  the frozen design-loop contract in ``tm/intent/design_loop.py``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    IntentSessionBody,
    validate_intent_session_spec,
    verify,
)
from tm.artifacts.models import (
    _DESIGN_STEPS,
    _SESSION_STATUSES,
    _TURN_ACTIONS,
    _TURN_ROLES,
)
from tm.artifacts.validator import ArtifactValidationError
from tm.intent.design_loop import DesignStep, SessionStatus, TurnAction

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "tm" / "artifacts" / "schemas" / "v0"


def _file_validator() -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / "intent_session.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _clean_body_raw() -> Dict[str, Any]:
    return {
        "session_id": "session.anon_fairness.v1",
        "root_intent_ref": "intent.anon_fairness",
        "status": "working",
        "current_step": "check_5w1h",
        "turns": [
            {"seq": 0, "role": "human", "action": "note", "output_ref": "blob.req.v1"},
            {"seq": 1, "role": "agent", "action": "propose", "provider": "fake", "output_ref": "pattern.cand.v1"},
        ],
        "produced_refs": ["pattern.cand.v1"],
        "metadata": {"owner": "pm"},
    }


def _envelope(session_id: str, *, status: str = "candidate", version: str = "v0.4") -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=f"session.{session_id}",
        status=ArtifactStatus(status),
        artifact_type=ArtifactType.INTENT_SESSION,
        version=version,
        created_by="tester",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )


def _candidate(body_raw: Dict[str, Any]) -> Artifact:
    body = IntentSessionBody.from_mapping(body_raw)
    return Artifact(envelope=_envelope(body.session_id), body=body, body_raw=body_raw)


# ─── File-level JSON schema ─────────────────────────────────────────


class TestIntentSessionFileSchema:
    def setup_method(self) -> None:
        self.validator = _file_validator()

    def test_minimal_session_validates(self) -> None:
        payload = {
            "session_id": "session.demo",
            "root_intent_ref": "intent.demo",
            "status": "working",
            "current_step": "draft",
        }
        assert list(self.validator.iter_errors(payload)) == []

    def test_full_session_validates(self) -> None:
        payload = _clean_body_raw()
        payload["completeness"] = {"overall": "partial"}
        assert list(self.validator.iter_errors(payload)) == []

    def test_sealed_session_with_sign_off_validates(self) -> None:
        payload = _clean_body_raw()
        payload["status"] = "sealed"
        payload["current_step"] = "sealed"
        payload["sign_off"] = {"signer": "pm@example.com", "scope": ["pattern.cand.v1"]}
        assert list(self.validator.iter_errors(payload)) == []

    def test_rejects_unknown_status(self) -> None:
        payload = _clean_body_raw()
        payload["status"] = "frozen"
        assert list(self.validator.iter_errors(payload)) != []

    def test_rejects_additional_properties(self) -> None:
        payload = _clean_body_raw()
        payload["surprise"] = True
        assert list(self.validator.iter_errors(payload)) != []


# ─── AST-level in-code schema ───────────────────────────────────────


class TestIntentSessionAstSchema:
    def test_valid_payload_passes(self) -> None:
        validate_intent_session_spec(_clean_body_raw())

    def test_missing_required_field_raises(self) -> None:
        payload = _clean_body_raw()
        del payload["root_intent_ref"]
        with pytest.raises(ArtifactValidationError):
            validate_intent_session_spec(payload)

    def test_bad_turn_action_raises(self) -> None:
        payload = _clean_body_raw()
        payload["turns"][0]["action"] = "teleport"
        with pytest.raises(ArtifactValidationError):
            validate_intent_session_spec(payload)


# ─── Dataclass parsing ──────────────────────────────────────────────


class TestIntentSessionFromMapping:
    def test_parses_turns_and_optionals(self) -> None:
        body = IntentSessionBody.from_mapping(_clean_body_raw())
        assert body.session_id == "session.anon_fairness.v1"
        assert len(body.turns) == 2
        assert body.turns[1].provider == "fake"
        assert body.sign_off is None
        assert body.completeness is None

    def test_rejects_bad_status(self) -> None:
        payload = _clean_body_raw()
        payload["status"] = "archived"
        with pytest.raises(ValueError):
            IntentSessionBody.from_mapping(payload)

    def test_rejects_bad_current_step(self) -> None:
        payload = _clean_body_raw()
        payload["current_step"] = "ship"
        with pytest.raises(ValueError):
            IntentSessionBody.from_mapping(payload)

    def test_rejects_non_int_seq(self) -> None:
        payload = _clean_body_raw()
        payload["turns"][0]["seq"] = "first"
        with pytest.raises(TypeError):
            IntentSessionBody.from_mapping(payload)


# ─── verify lifecycle ───────────────────────────────────────────────


class TestIntentSessionVerify:
    def test_clean_working_candidate_verifies(self) -> None:
        accepted, report = verify(_candidate(_clean_body_raw()))
        assert accepted is not None, report.errors
        assert report.errors == []
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED

    def test_body_hash_stamped(self) -> None:
        accepted, _ = verify(_candidate(_clean_body_raw()))
        assert accepted is not None
        assert len(accepted.envelope.body_hash) == 64

    def test_idempotent_reverify(self) -> None:
        body_raw = _clean_body_raw()
        acc1, _ = verify(_candidate(body_raw))
        acc2, _ = verify(_candidate(body_raw))
        assert acc1 is not None and acc2 is not None
        assert acc1.envelope.body_hash == acc2.envelope.body_hash

    def test_rejects_non_monotonic_seq(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["turns"][1]["seq"] = 0  # duplicate of turn[0]
        accepted, report = verify(_candidate(body_raw))
        assert accepted is None
        assert any("strictly increasing" in err for err in report.errors)

    def test_rejects_sealed_without_sign_off(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["status"] = "sealed"
        body_raw["current_step"] = "sealed"
        accepted, report = verify(_candidate(body_raw))
        assert accepted is None
        assert any("sign_off" in err for err in report.errors)

    def test_accepts_sealed_with_sign_off(self) -> None:
        body_raw = _clean_body_raw()
        body_raw["status"] = "sealed"
        body_raw["current_step"] = "sealed"
        body_raw["sign_off"] = {"signer": "pm@example.com", "scope": ["pattern.cand.v1"]}
        accepted, report = verify(_candidate(body_raw))
        assert accepted is not None, report.errors

    def test_rejects_non_candidate_status(self) -> None:
        body_raw = _clean_body_raw()
        body = IntentSessionBody.from_mapping(body_raw)
        env = _envelope(body.session_id, status="accepted")
        accepted, report = verify(Artifact(envelope=env, body=body, body_raw=body_raw))
        assert accepted is None
        assert any("status" in err for err in report.errors)


# ─── Vocabulary drift guard ─────────────────────────────────────────


def test_body_vocabulary_matches_design_loop_contract() -> None:
    """The body's local frozensets must never drift from the frozen contract."""
    assert _SESSION_STATUSES == {s.value for s in SessionStatus}
    assert _DESIGN_STEPS == {s.value for s in DesignStep}
    assert _TURN_ACTIONS == {a.value for a in TurnAction}
    assert _TURN_ROLES == {"human", "agent"}
