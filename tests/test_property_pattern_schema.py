"""Tests for PropertyPattern v0.2 — schema, AST validator, and dataclass body.

Covers Stage 5-1.2 of Phase 5: the new ``kind: PropertyPattern`` artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    ArtifactType,
    ArtifactValidationError,
    PropertyPatternBody,
    PropertyPatternCounterexample,
    PropertyPatternSlot,
    validate_property_pattern_spec,
)

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "tm" / "artifacts" / "schemas" / "v0"


@pytest.fixture(scope="module")
def file_schema_validator() -> Draft202012Validator:
    payload = json.loads((_SCHEMAS_DIR / "property_pattern.json").read_text())
    return Draft202012Validator(payload, format_checker=FormatChecker())


def _minimal_payload() -> dict:
    return {
        "pattern_id": "safety.no_x_amplifies_y",
        "category": "safety",
        "title": "No coordinated amplification",
        "formula_template": "AG(~controlled[{actor}].amplifies[{content}])",
        "slots": [
            {"name": "actor", "type": "Actor"},
            {"name": "content", "type": "Content"},
        ],
    }


def _full_payload() -> dict:
    return {
        **_minimal_payload(),
        "description": "Coordinated actors must not amplify protected content set.",
        "applicable_conditions": [
            "Actor identity registry is observable",
            "Amplification events are traced",
        ],
        "counterexamples": [
            {
                "description": "A cluster sustains amplification over a 24h window",
                "scenario": "actors A,B,C amplify content X at coordinated cadence",
            }
        ],
        "metadata": {"author": "tracemind.seed", "version": "1.0"},
    }


class TestFileLevelSchema:
    def test_accepts_minimal(self, file_schema_validator: Draft202012Validator) -> None:
        errors = list(file_schema_validator.iter_errors(_minimal_payload()))
        assert errors == []

    def test_accepts_full(self, file_schema_validator: Draft202012Validator) -> None:
        errors = list(file_schema_validator.iter_errors(_full_payload()))
        assert errors == []

    @pytest.mark.parametrize(
        "missing_field",
        ["pattern_id", "category", "title", "formula_template", "slots"],
    )
    def test_rejects_missing_required(self, file_schema_validator: Draft202012Validator, missing_field: str) -> None:
        payload = _minimal_payload()
        del payload[missing_field]
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors, f"expected schema error for missing {missing_field}"
        assert any(missing_field in err.message for err in errors)

    def test_rejects_invalid_category(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["category"] = "performance"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_empty_slots(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["slots"] = []
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_additional_top_level_property(self, file_schema_validator: Draft202012Validator) -> None:
        payload = {**_minimal_payload(), "unknown_field": "x"}
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors

    def test_rejects_bad_slot_name_pattern(self, file_schema_validator: Draft202012Validator) -> None:
        payload = _minimal_payload()
        payload["slots"][0]["name"] = "ActorWithCamel"
        errors = list(file_schema_validator.iter_errors(payload))
        assert errors


class TestASTValidator:
    def test_accepts_minimal(self) -> None:
        validate_property_pattern_spec(_minimal_payload())

    def test_accepts_full(self) -> None:
        validate_property_pattern_spec(_full_payload())

    @pytest.mark.parametrize(
        "missing_field",
        ["pattern_id", "category", "title", "formula_template", "slots"],
    )
    def test_rejects_missing_required(self, missing_field: str) -> None:
        payload = _minimal_payload()
        del payload[missing_field]
        with pytest.raises(ArtifactValidationError) as excinfo:
            validate_property_pattern_spec(payload)
        assert missing_field in str(excinfo.value)

    def test_rejects_invalid_category(self) -> None:
        payload = _minimal_payload()
        payload["category"] = "performance"
        with pytest.raises(ArtifactValidationError):
            validate_property_pattern_spec(payload)


class TestPropertyPatternBody:
    def test_artifact_type_constant(self) -> None:
        assert ArtifactType.PROPERTY_PATTERN.value == "property_pattern"
        assert PropertyPatternBody.artifact_type is ArtifactType.PROPERTY_PATTERN

    def test_from_mapping_minimal(self) -> None:
        body = PropertyPatternBody.from_mapping(_minimal_payload())
        assert body.pattern_id == "safety.no_x_amplifies_y"
        assert body.category == "safety"
        assert body.title == "No coordinated amplification"
        assert len(body.slots) == 2
        assert body.slots[0].name == "actor"
        assert body.slots[0].required is True
        assert body.description is None
        assert body.applicable_conditions == []
        assert body.counterexamples == []
        assert body.metadata == {}

    def test_from_mapping_full(self) -> None:
        body = PropertyPatternBody.from_mapping(_full_payload())
        assert body.description is not None
        assert len(body.applicable_conditions) == 2
        assert len(body.counterexamples) == 1
        assert isinstance(body.counterexamples[0], PropertyPatternCounterexample)
        assert body.counterexamples[0].scenario is not None
        assert body.metadata["author"] == "tracemind.seed"

    def test_from_mapping_rejects_invalid_category(self) -> None:
        payload = _minimal_payload()
        payload["category"] = "performance"
        with pytest.raises(ValueError, match="category"):
            PropertyPatternBody.from_mapping(payload)

    def test_from_mapping_rejects_empty_slots(self) -> None:
        payload = _minimal_payload()
        payload["slots"] = []
        with pytest.raises(ValueError, match="slots"):
            PropertyPatternBody.from_mapping(payload)

    def test_slot_required_defaults_to_true(self) -> None:
        slot = PropertyPatternSlot.from_mapping({"name": "actor", "type": "Actor"})
        assert slot.required is True

    def test_slot_required_respects_explicit_false(self) -> None:
        slot = PropertyPatternSlot.from_mapping({"name": "actor", "type": "Actor", "required": False})
        assert slot.required is False
