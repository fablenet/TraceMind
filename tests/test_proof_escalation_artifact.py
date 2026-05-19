"""Tests for ProofReport / EscalationReport artifact bodies (Stage 5-2 task 2.5).

Promotes the runtime classes in ``tm.control.meta.{proof,escalation}`` to
K-Ontology v0.2 artifact kinds. Covers:

- File-level JSON schema validation (round-trip)
- AST-level Python schema validation
- Dataclass parsing via ``from_mapping``
- Phase 6 reserved fields (``peer_node_id`` / ``peer_chain_ref``) are
  optional and pass through cleanly
- v0.1 backward compat: existing artifacts still validate
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    ArtifactType,
    EscalationReportBody,
    EscalationVerdictBody,
    EvidenceEntryBody,
    KripkeVerdictBody,
    ProofReportBody,
    validate_escalation_report_spec,
    validate_proof_report_spec,
)
from tm.artifacts.validator import ArtifactValidationError

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "tm" / "artifacts" / "schemas" / "v0"


def _file_validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


# ─── ProofReport ───────────────────────────────────────────────────


class TestProofReportFileSchema:
    def setup_method(self) -> None:
        self.validator = _file_validator("proof_report.json")

    def test_minimal_proof_report_validates(self) -> None:
        payload = {
            "report_id": "proof-001",
            "intent_id": "test.intent",
            "cycle_id": "cycle-001",
            "overall_verdict": "pass",
        }
        errors = list(self.validator.iter_errors(payload))
        assert errors == []

    def test_full_proof_report_validates(self) -> None:
        payload = {
            "report_id": "proof-001",
            "intent_id": "test.intent",
            "cycle_id": "cycle-001",
            "pre_snapshot": {"x": 1},
            "post_snapshot": {"x": 2},
            "execution_summary": {"status": "succeeded"},
            "kripke_verdict": {
                "verified": True,
                "properties_checked": 3,
                "properties_passed": 3,
                "failed_properties": [],
                "counterexamples": [],
            },
            "evidence_chain": [
                {
                    "source": "controller_cycle",
                    "event_type": "cycle_completed",
                    "data": {"bundle": "b-1"},
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ],
            "policy_decisions": [{"effect_ref": "x:throttle", "allowed": True}],
            "overall_verdict": "pass",
            "verdict_reason": "all checks passed",
            "created_at": "2026-01-01T00:00:00Z",
            "report_hash": "sha256:deadbeef",
            "metadata": {"author": "test"},
        }
        assert list(self.validator.iter_errors(payload)) == []

    def test_missing_required_field_fails(self) -> None:
        payload = {
            "intent_id": "test.intent",
            "cycle_id": "cycle-001",
            "overall_verdict": "pass",
        }
        errors = list(self.validator.iter_errors(payload))
        assert any("report_id" in str(err.message) for err in errors)

    def test_invalid_overall_verdict_enum_fails(self) -> None:
        payload = {
            "report_id": "p",
            "intent_id": "i",
            "cycle_id": "c",
            "overall_verdict": "maybe",
        }
        errors = list(self.validator.iter_errors(payload))
        assert any("maybe" in str(err.message) for err in errors)

    def test_extra_field_rejected(self) -> None:
        payload = {
            "report_id": "p",
            "intent_id": "i",
            "cycle_id": "c",
            "overall_verdict": "pass",
            "rogue_field": True,
        }
        errors = list(self.validator.iter_errors(payload))
        assert any("rogue_field" in str(err.message) for err in errors)

    def test_phase_6_peer_fields_accepted_as_optional(self) -> None:
        payload = {
            "report_id": "p",
            "intent_id": "i",
            "cycle_id": "c",
            "overall_verdict": "pass",
            "peer_node_id": "star.leaf.host-1",
            "peer_chain_ref": "sha256:abc",
        }
        assert list(self.validator.iter_errors(payload)) == []


class TestProofReportAstSchema:
    def test_validate_proof_report_spec_minimal(self) -> None:
        validate_proof_report_spec(
            {
                "report_id": "p",
                "intent_id": "i",
                "cycle_id": "c",
                "overall_verdict": "pass",
            }
        )

    def test_validate_proof_report_spec_rejects_invalid_verdict(self) -> None:
        with pytest.raises(ArtifactValidationError):
            validate_proof_report_spec(
                {
                    "report_id": "p",
                    "intent_id": "i",
                    "cycle_id": "c",
                    "overall_verdict": "garbage",
                }
            )

    def test_validate_proof_report_spec_rejects_missing_required(self) -> None:
        with pytest.raises(ArtifactValidationError):
            validate_proof_report_spec(
                {
                    "intent_id": "i",
                    "cycle_id": "c",
                    "overall_verdict": "pass",
                }
            )


class TestProofReportBodyDataclass:
    def test_from_mapping_minimal(self) -> None:
        body = ProofReportBody.from_mapping(
            {
                "report_id": "p",
                "intent_id": "i",
                "cycle_id": "c",
                "overall_verdict": "pass",
            }
        )
        assert body.artifact_type == ArtifactType.PROOF_REPORT
        assert body.report_id == "p"
        assert body.kripke_verdict is None
        assert body.evidence_chain == []
        assert body.peer_node_id is None
        assert body.peer_chain_ref is None

    def test_from_mapping_with_kripke_and_evidence(self) -> None:
        body = ProofReportBody.from_mapping(
            {
                "report_id": "p",
                "intent_id": "i",
                "cycle_id": "c",
                "overall_verdict": "fail",
                "kripke_verdict": {
                    "verified": False,
                    "properties_checked": 2,
                    "properties_passed": 1,
                    "failed_properties": ["safety.A"],
                },
                "evidence_chain": [
                    {"source": "s1", "event_type": "e1"},
                    {"source": "s2", "event_type": "e2", "data": {"k": "v"}},
                ],
            }
        )
        assert isinstance(body.kripke_verdict, KripkeVerdictBody)
        assert body.kripke_verdict.failed_properties == ["safety.A"]
        assert len(body.evidence_chain) == 2
        assert isinstance(body.evidence_chain[0], EvidenceEntryBody)
        assert body.evidence_chain[0].source == "s1"

    def test_phase_6_peer_fields_round_trip(self) -> None:
        body = ProofReportBody.from_mapping(
            {
                "report_id": "p",
                "intent_id": "i",
                "cycle_id": "c",
                "overall_verdict": "pass",
                "peer_node_id": "star.leaf.host-1",
                "peer_chain_ref": "sha256:abc123",
            }
        )
        assert body.peer_node_id == "star.leaf.host-1"
        assert body.peer_chain_ref == "sha256:abc123"

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required field.*report_id"):
            ProofReportBody.from_mapping(
                {
                    "intent_id": "i",
                    "cycle_id": "c",
                    "overall_verdict": "pass",
                }
            )

    def test_invalid_evidence_chain_shape_raises(self) -> None:
        with pytest.raises(TypeError):
            ProofReportBody.from_mapping(
                {
                    "report_id": "p",
                    "intent_id": "i",
                    "cycle_id": "c",
                    "overall_verdict": "pass",
                    "evidence_chain": "not-a-list",
                }
            )


# ─── EscalationReport ─────────────────────────────────────────────


class TestEscalationReportFileSchema:
    def setup_method(self) -> None:
        self.validator = _file_validator("escalation_report.json")

    def test_minimal_escalation_validates(self) -> None:
        payload = {
            "report_id": "esc-001",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "warning",
            "intent_ref": "my.intent",
        }
        assert list(self.validator.iter_errors(payload)) == []

    def test_full_escalation_validates(self) -> None:
        payload = {
            "report_id": "esc-001",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "critical",
            "intent_ref": "my.intent",
            "verdicts": [
                {
                    "kpi": "error_rate",
                    "trend": "worsening",
                    "converged": False,
                    "delta": 0.3,
                    "values": [0.1, 0.3, 0.6],
                    "reason": "metric trending up",
                }
            ],
            "kpi_history_count": 3,
            "recent_rules_fired": ["r1", "r2"],
            "recent_errors": ["err1"],
            "gap_summary": "policy not responding",
            "suggested_actions": ["human_review", "add_new_rule"],
            "counterexample": {"description": "adversary adapted"},
            "metadata": {"author": "test"},
        }
        assert list(self.validator.iter_errors(payload)) == []

    def test_invalid_severity_enum_fails(self) -> None:
        payload = {
            "report_id": "e",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "panic",
            "intent_ref": "i",
        }
        errors = list(self.validator.iter_errors(payload))
        assert any("panic" in str(err.message) for err in errors)

    def test_invalid_suggested_action_fails(self) -> None:
        payload = {
            "report_id": "e",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "warning",
            "intent_ref": "i",
            "suggested_actions": ["call_the_ceo"],
        }
        errors = list(self.validator.iter_errors(payload))
        assert any("call_the_ceo" in str(err.message) for err in errors)

    def test_counterexample_can_be_null(self) -> None:
        payload = {
            "report_id": "e",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "info",
            "intent_ref": "i",
            "counterexample": None,
        }
        assert list(self.validator.iter_errors(payload)) == []

    def test_phase_6_peer_node_id_accepted(self) -> None:
        payload = {
            "report_id": "e",
            "timestamp": "2026-01-01T00:00:00Z",
            "severity": "warning",
            "intent_ref": "i",
            "peer_node_id": "star.leaf.host-1",
        }
        assert list(self.validator.iter_errors(payload)) == []


class TestEscalationReportAstSchema:
    def test_validate_minimal(self) -> None:
        validate_escalation_report_spec(
            {
                "report_id": "e",
                "timestamp": "2026-01-01T00:00:00Z",
                "severity": "warning",
                "intent_ref": "i",
            }
        )

    def test_validate_rejects_bad_severity(self) -> None:
        with pytest.raises(ArtifactValidationError):
            validate_escalation_report_spec(
                {
                    "report_id": "e",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "severity": "bad",
                    "intent_ref": "i",
                }
            )

    def test_validate_rejects_bad_trend(self) -> None:
        with pytest.raises(ArtifactValidationError):
            validate_escalation_report_spec(
                {
                    "report_id": "e",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "severity": "warning",
                    "intent_ref": "i",
                    "verdicts": [{"kpi": "x", "trend": "weird"}],
                }
            )


class TestEscalationReportBodyDataclass:
    def test_from_mapping_minimal(self) -> None:
        body = EscalationReportBody.from_mapping(
            {
                "report_id": "e",
                "timestamp": "2026-01-01T00:00:00Z",
                "severity": "warning",
                "intent_ref": "i",
            }
        )
        assert body.artifact_type == ArtifactType.ESCALATION_REPORT
        assert body.verdicts == []
        assert body.peer_node_id is None

    def test_verdicts_parse(self) -> None:
        body = EscalationReportBody.from_mapping(
            {
                "report_id": "e",
                "timestamp": "2026-01-01T00:00:00Z",
                "severity": "critical",
                "intent_ref": "i",
                "verdicts": [
                    {
                        "kpi": "error_rate",
                        "trend": "worsening",
                        "values": [0.1, 0.3, 0.6],
                    }
                ],
            }
        )
        assert len(body.verdicts) == 1
        assert isinstance(body.verdicts[0], EscalationVerdictBody)
        assert body.verdicts[0].kpi == "error_rate"
        assert body.verdicts[0].values == [0.1, 0.3, 0.6]

    def test_phase_6_peer_node_id_round_trip(self) -> None:
        body = EscalationReportBody.from_mapping(
            {
                "report_id": "e",
                "timestamp": "2026-01-01T00:00:00Z",
                "severity": "warning",
                "intent_ref": "i",
                "peer_node_id": "star.center",
            }
        )
        assert body.peer_node_id == "star.center"


# ─── Envelope enum sync ───────────────────────────────────────────


class TestEnvelopeEnumSync:
    def test_envelope_accepts_proof_report_artifact_type(self) -> None:
        envelope_schema = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
        types_enum = envelope_schema["properties"]["artifact_type"]["enum"]
        assert "proof_report" in types_enum

    def test_envelope_accepts_escalation_report_artifact_type(self) -> None:
        envelope_schema = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
        types_enum = envelope_schema["properties"]["artifact_type"]["enum"]
        assert "escalation_report" in types_enum

    def test_all_artifact_types_in_envelope_enum(self) -> None:
        envelope_schema = json.loads((SCHEMAS_DIR / "envelope.json").read_text(encoding="utf-8"))
        types_enum = set(envelope_schema["properties"]["artifact_type"]["enum"])
        expected = {t.value for t in ArtifactType}
        assert types_enum == expected
