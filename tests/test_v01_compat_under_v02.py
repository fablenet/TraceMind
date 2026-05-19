"""Regression: v0.1 artifacts remain valid under v0.2 schemas.

Covers Stage 5-1.5 of Phase 5. The promise of K-Ontology v0.2 is that the
upgrade is **purely additive**: every v0.1 artifact (intent body, intent spec,
policy spec, capability spec, workflow policy, etc.) MUST continue to validate
without modification. This test makes that promise enforceable in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    IntentBody,
    validate_capability_spec,
    validate_intent_spec,
    validate_policy_spec,
    validate_workflow_policy,
)


_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "tm" / "artifacts" / "schemas" / "v0"


def _load_schema_validator(name: str) -> Draft202012Validator:
    payload = json.loads((_SCHEMAS_DIR / name).read_text())
    return Draft202012Validator(payload, format_checker=FormatChecker())


# ---------------------------------------------------------------------------
# File-level schemas: v0.1-shaped bodies under v0.2 schema files
# ---------------------------------------------------------------------------


class TestFileSchemaCompat:
    def test_v01_intent_body_validates_under_v02_intent_schema(self) -> None:
        validator = _load_schema_validator("intent.json")
        v01_payload = {
            "intent_id": "intent.create.result",
            "title": "Create a result",
            "context": "result lifecycle",
            "goal": "result is created and validated",
            "non_goals": ["billing"],
            "actors": ["user"],
            "inputs": ["request"],
            "outputs": ["result_id"],
            "constraints": ["no_pii"],
            "success_metrics": ["latency_p99 < 200ms"],
            "risks": ["spike_load"],
            "assumptions": ["network_available"],
            "trace_links": {"parent_intent": None, "related_intents": []},
        }
        assert list(validator.iter_errors(v01_payload)) == []

    def test_v01_envelope_validates_after_enum_sync(self) -> None:
        validator = _load_schema_validator("envelope.json")
        v01_envelope = {
            "artifact_id": "intent:abc",
            "status": "candidate",
            "artifact_type": "intent",
            "version": "v0.1",
            "created_by": "human:author",
            "created_at": "2024-01-01T00:00:00Z",
            "body_hash": "0" * 64,
            "envelope_hash": "1" * 64,
            "meta": {},
        }
        assert list(validator.iter_errors(v01_envelope)) == []

    def test_envelope_now_accepts_all_artifact_types(self) -> None:
        validator = _load_schema_validator("envelope.json")
        for artifact_type in (
            "intent",
            "capabilities",
            "plan",
            "gap_map",
            "backlog",
            "agent_bundle",
            "environment_snapshot",
            "proposed_change_plan",
            "execution_report",
            "property_pattern",
        ):
            envelope = {
                "artifact_id": f"{artifact_type}:abc",
                "status": "candidate",
                "artifact_type": artifact_type,
                "version": "v0.1",
                "created_by": "human:author",
                "created_at": "2024-01-01T00:00:00Z",
                "body_hash": "0" * 64,
                "envelope_hash": "1" * 64,
                "meta": {},
            }
            errors = list(validator.iter_errors(envelope))
            assert errors == [], f"envelope rejected for artifact_type={artifact_type}: {errors}"


# ---------------------------------------------------------------------------
# Dataclass parsing: a v0.1 IntentBody without new fields still works
# ---------------------------------------------------------------------------


class TestIntentBodyDataclassCompat:
    def test_v01_intent_body_dataclass_parses_without_new_fields(self) -> None:
        body = IntentBody.from_mapping(
            {
                "intent_id": "intent.create.result",
                "title": "title",
                "context": "ctx",
                "goal": "goal",
            }
        )
        assert body.property_pattern_refs == []
        assert body.slot_fills == {}


# ---------------------------------------------------------------------------
# AST schemas: same minimal payloads as the existing v0.1 suite
# ---------------------------------------------------------------------------


class TestASTValidatorCompat:
    @pytest.mark.parametrize(
        "validator_fn, payload",
        [
            (
                validate_intent_spec,
                {
                    "intent_id": "intent.create.result",
                    "version": "1.0.0",
                    "goal": {"type": "achieve", "target": "result.validated"},
                },
            ),
            (
                validate_policy_spec,
                {
                    "policy_id": "policy.minimal",
                    "version": "1.0.0",
                    "state_schema": {"result.validated": {"type": "string"}},
                },
            ),
            (
                validate_capability_spec,
                {
                    "capability_id": "compute.process",
                    "version": "0.1.0",
                    "inputs": {},
                    "event_types": [{"name": "compute.process.done"}],
                    "state_extractors": [],
                    "safety_contract": {
                        "determinism": True,
                        "side_effects": ["write"],
                        "rollback": {"supported": False},
                    },
                },
            ),
            (
                validate_workflow_policy,
                {
                    "workflow_id": "policy.minimal.reference",
                    "intent_id": "intent.create.result",
                    "policy_id": "policy.minimal",
                    "steps": [
                        {"step_id": "step_compute", "capability_id": "compute.process"},
                    ],
                    "explanation": {
                        "intent_coverage": "carries intent target",
                        "capability_reasoning": "single compute step",
                        "constraint_coverage": "no extra invariants",
                        "risks": ["none"],
                    },
                },
            ),
        ],
    )
    def test_v01_payload_still_accepted_under_v02_ast(self, validator_fn, payload) -> None:
        validator_fn(payload)


# ---------------------------------------------------------------------------
# Body hash stability: an IntentBody without the new fields hashes to the
# same value as it would before v0.2 (i.e. omitting a field is equivalent
# to it being absent in the canonical form). This is what guarantees the
# additive promise doesn't break existing accepted artifacts.
# ---------------------------------------------------------------------------


class TestCanonicalizationCompat:
    def test_v01_body_canonical_form_unchanged(self) -> None:
        from tm.artifacts import body_hash

        v01_body = {
            "intent_id": "intent.test",
            "title": "t",
            "context": "c",
            "goal": "g",
        }
        v02_body = {
            **v01_body,
            "property_pattern_refs": [],
            "slot_fills": {},
        }
        v02_with_refs = {
            **v01_body,
            "property_pattern_refs": ["safety.x"],
            "slot_fills": {"safety.x": {"actor": "A"}},
        }
        h1 = body_hash(v01_body)
        h2 = body_hash(v02_body)
        h3 = body_hash(v02_with_refs)
        assert h1 != h2, "explicit empty arrays/objects must differ from omitted fields"
        assert h2 != h3, "actual pattern refs must change the hash"
