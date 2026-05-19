"""Tests for PropertyPattern lint rules.

Covers Stage 5-1.4 of Phase 5:

- ``lint_property_pattern`` (single-artifact internal consistency)
- ``lint_intent_pattern_refs`` (cross-artifact: Intent → PropertyPattern library)
"""

from __future__ import annotations

# Load tm.artifacts first to avoid a pre-existing circular import order issue
# when tm.lint is imported before tm.artifacts in a fresh interpreter.
import tm.artifacts  # noqa: F401  (import-for-side-effect)
from tm.lint import lint_intent_pattern_refs, lint_property_pattern


def _good_pattern() -> dict:
    return {
        "pattern_id": "safety.no_x_amplifies_y",
        "category": "safety",
        "title": "no amplification",
        "formula_template": "AG(~controlled[{actor}].amplifies[{content}])",
        "slots": [
            {"name": "actor", "type": "Actor"},
            {"name": "content", "type": "Content"},
        ],
    }


def _codes(issues) -> list[str]:
    return [issue.code for issue in issues]


class TestLintPropertyPattern:
    def test_good_pattern_no_issues(self) -> None:
        assert lint_property_pattern(_good_pattern()) == []

    def test_undeclared_placeholder(self) -> None:
        pattern = _good_pattern()
        pattern["formula_template"] = "AG({foo} & ~{actor})"
        codes = _codes(lint_property_pattern(pattern))
        assert "PP_SLOT_UNDECLARED" in codes

    def test_unused_slot(self) -> None:
        pattern = _good_pattern()
        pattern["formula_template"] = "AG(~controlled[{actor}].active)"
        issues = lint_property_pattern(pattern)
        unused = [i for i in issues if i.code == "PP_SLOT_UNUSED"]
        assert unused
        assert unused[0].severity == "warning"

    def test_duplicate_slot_name(self) -> None:
        pattern = _good_pattern()
        pattern["slots"].append({"name": "actor", "type": "Actor"})
        codes = _codes(lint_property_pattern(pattern))
        assert "PP_SLOT_DUPLICATE" in codes

    def test_bad_slot_name_pattern(self) -> None:
        pattern = _good_pattern()
        pattern["slots"][0]["name"] = "BadCamelName"
        codes = _codes(lint_property_pattern(pattern))
        assert "PP_SLOT_NAME" in codes

    def test_pattern_id_category_mismatch_is_warning(self) -> None:
        pattern = _good_pattern()
        pattern["pattern_id"] = "wrong.prefix.something"
        issues = lint_property_pattern(pattern)
        cat_issues = [i for i in issues if i.code == "PP_ID_CATEGORY"]
        assert cat_issues
        assert cat_issues[0].severity == "warning"

    def test_pattern_id_category_match_no_warning(self) -> None:
        pattern = _good_pattern()
        pattern["pattern_id"] = "safety.something_else"
        issues = lint_property_pattern(pattern)
        assert not any(i.code == "PP_ID_CATEGORY" for i in issues)

    def test_liveness_category_prefix(self) -> None:
        pattern = {
            "pattern_id": "liveness.eventually_done",
            "category": "liveness",
            "title": "eventually completes",
            "formula_template": "AF({task}.done)",
            "slots": [{"name": "task", "type": "Task"}],
        }
        assert lint_property_pattern(pattern) == []


class TestLintIntentPatternRefs:
    def _library(self) -> dict:
        return {"safety.no_x_amplifies_y": _good_pattern()}

    def test_well_formed_intent_no_issues(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {
                "safety.no_x_amplifies_y": {
                    "actor": "Actor:foo",
                    "content": "Content:bar",
                }
            },
        }
        assert lint_intent_pattern_refs(intent, self._library()) == []

    def test_empty_intent_no_issues(self) -> None:
        intent = {"property_pattern_refs": [], "slot_fills": {}}
        assert lint_intent_pattern_refs(intent, self._library()) == []

    def test_unknown_pattern_ref(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.missing"],
            "slot_fills": {"safety.missing": {}},
        }
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_PATTERN_UNKNOWN" in codes

    def test_missing_required_slot(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {"safety.no_x_amplifies_y": {"actor": "Actor:foo"}},
        }
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_SLOT_FILL_MISSING" in codes

    def test_missing_required_slot_when_no_fill_entry(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {},
        }
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_SLOT_FILL_MISSING" in codes

    def test_extraneous_slot_fill(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {
                "safety.no_x_amplifies_y": {
                    "actor": "Actor:foo",
                    "content": "Content:bar",
                    "extra_slot": "x",
                }
            },
        }
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_SLOT_FILL_EXTRA" in codes

    def test_orphan_slot_fill(self) -> None:
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {
                "safety.no_x_amplifies_y": {
                    "actor": "Actor:foo",
                    "content": "Content:bar",
                },
                "safety.orphan": {"actor": "Actor:foo"},
            },
        }
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_SLOT_FILL_ORPHAN" in codes

    def test_optional_slot_not_required(self) -> None:
        library = {
            "safety.with_optional": {
                **_good_pattern(),
                "pattern_id": "safety.with_optional",
                "slots": [
                    {"name": "actor", "type": "Actor"},
                    {"name": "content", "type": "Content", "required": False},
                ],
            }
        }
        intent = {
            "property_pattern_refs": ["safety.with_optional"],
            "slot_fills": {"safety.with_optional": {"actor": "Actor:foo"}},
        }
        assert lint_intent_pattern_refs(intent, library) == []

    def test_non_list_pattern_refs_shape_error(self) -> None:
        intent = {"property_pattern_refs": "wrong", "slot_fills": {}}
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_PATTERN_REFS_SHAPE" in codes

    def test_non_mapping_slot_fills_shape_error(self) -> None:
        intent = {"property_pattern_refs": [], "slot_fills": ["wrong"]}
        codes = _codes(lint_intent_pattern_refs(intent, self._library()))
        assert "INTENT_SLOT_FILLS_SHAPE" in codes

    def test_multiple_pattern_refs(self) -> None:
        library = {
            **self._library(),
            "fairness.eq": {
                "pattern_id": "fairness.eq",
                "category": "fairness",
                "title": "equal",
                "formula_template": "{a} = {b}",
                "slots": [
                    {"name": "a", "type": "X"},
                    {"name": "b", "type": "X"},
                ],
            },
        }
        intent = {
            "property_pattern_refs": ["safety.no_x_amplifies_y", "fairness.eq"],
            "slot_fills": {
                "safety.no_x_amplifies_y": {
                    "actor": "Actor:foo",
                    "content": "Content:bar",
                },
                "fairness.eq": {"a": "A", "b": "B"},
            },
        }
        assert lint_intent_pattern_refs(intent, library) == []
