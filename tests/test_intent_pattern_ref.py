"""Tests for IntentBody / IntentSpec additive ``property_pattern_refs`` + ``slot_fills``.

Covers Stage 5-1.3 of Phase 5: the IntentTree additive extension that lets an
Intent declare which PropertyPatterns it implements.

Key compatibility invariant: a v0.1 IntentBody that omits both new fields must
remain fully valid and parseable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tm.artifacts import (
    ArtifactValidationError,
    IntentBody,
    validate_intent_spec,
)

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "tm" / "artifacts" / "schemas" / "v0"


@pytest.fixture(scope="module")
def intent_body_schema() -> Draft202012Validator:
    payload = json.loads((_SCHEMAS_DIR / "intent.json").read_text())
    return Draft202012Validator(payload, format_checker=FormatChecker())


def _intent_body_v01() -> dict:
    return {
        "intent_id": "intent.create.result",
        "title": "Create a result",
        "context": "result lifecycle",
        "goal": "result is created and validated",
    }


def _intent_body_v02() -> dict:
    return {
        **_intent_body_v01(),
        "property_pattern_refs": [
            "safety.no_x_amplifies_y",
            "fairness.equal_opportunity",
        ],
        "slot_fills": {
            "safety.no_x_amplifies_y": {
                "actor": "Actor:author_pub",
                "content": "Content:protected_set",
            },
            "fairness.equal_opportunity": {
                "group_a": "Tenant:premium",
                "group_b": "Tenant:standard",
            },
        },
    }


class TestIntentBodyFileSchema:
    def test_accepts_v01_minimal(self, intent_body_schema: Draft202012Validator) -> None:
        assert list(intent_body_schema.iter_errors(_intent_body_v01())) == []

    def test_accepts_v02_with_pattern_refs(self, intent_body_schema: Draft202012Validator) -> None:
        assert list(intent_body_schema.iter_errors(_intent_body_v02())) == []

    def test_accepts_empty_pattern_refs_and_fills(self, intent_body_schema: Draft202012Validator) -> None:
        payload = {**_intent_body_v01(), "property_pattern_refs": [], "slot_fills": {}}
        assert list(intent_body_schema.iter_errors(payload)) == []

    def test_rejects_non_list_pattern_refs(self, intent_body_schema: Draft202012Validator) -> None:
        payload = {**_intent_body_v01(), "property_pattern_refs": "safety.x"}
        assert list(intent_body_schema.iter_errors(payload))

    def test_rejects_non_object_slot_fills(self, intent_body_schema: Draft202012Validator) -> None:
        payload = {**_intent_body_v01(), "slot_fills": "wrong"}
        assert list(intent_body_schema.iter_errors(payload))

    def test_rejects_slot_fills_inner_non_object(self, intent_body_schema: Draft202012Validator) -> None:
        payload = {**_intent_body_v01(), "slot_fills": {"safety.x": "wrong"}}
        assert list(intent_body_schema.iter_errors(payload))


class TestIntentBodyDataclass:
    def test_v01_compat_no_new_fields(self) -> None:
        body = IntentBody.from_mapping(_intent_body_v01())
        assert body.property_pattern_refs == []
        assert body.slot_fills == {}

    def test_v02_parses_pattern_refs_and_slot_fills(self) -> None:
        body = IntentBody.from_mapping(_intent_body_v02())
        assert body.property_pattern_refs == [
            "safety.no_x_amplifies_y",
            "fairness.equal_opportunity",
        ]
        assert body.slot_fills["safety.no_x_amplifies_y"]["actor"] == "Actor:author_pub"
        assert body.slot_fills["fairness.equal_opportunity"]["group_b"] == "Tenant:standard"

    def test_rejects_non_mapping_slot_fills(self) -> None:
        payload = {**_intent_body_v01(), "slot_fills": ["wrong"]}
        with pytest.raises(TypeError, match="slot_fills"):
            IntentBody.from_mapping(payload)

    def test_rejects_inner_non_mapping_slot_fills(self) -> None:
        payload = {**_intent_body_v01(), "slot_fills": {"safety.x": "wrong"}}
        with pytest.raises(TypeError, match="slot_fills"):
            IntentBody.from_mapping(payload)


class TestIntentSpecASTValidator:
    def _spec_v01(self) -> dict:
        return {
            "intent_id": "intent.create.result",
            "version": "1.0.0",
            "goal": {"type": "achieve", "target": "result.validated"},
        }

    def test_v01_compat(self) -> None:
        validate_intent_spec(self._spec_v01())

    def test_v02_with_pattern_refs(self) -> None:
        payload = {
            **self._spec_v01(),
            "property_pattern_refs": ["safety.no_x"],
            "slot_fills": {"safety.no_x": {"actor": "Actor:foo"}},
        }
        validate_intent_spec(payload)

    def test_rejects_non_string_pattern_ref(self) -> None:
        payload = {**self._spec_v01(), "property_pattern_refs": [123]}
        with pytest.raises(ArtifactValidationError):
            validate_intent_spec(payload)

    def test_rejects_non_object_slot_fills_value(self) -> None:
        payload = {**self._spec_v01(), "slot_fills": {"safety.x": "wrong"}}
        with pytest.raises(ArtifactValidationError):
            validate_intent_spec(payload)
