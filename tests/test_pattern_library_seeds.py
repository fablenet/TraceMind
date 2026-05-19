"""Tests for ``tm.patterns.library`` — seed PropertyPattern discovery.

Covers:
- All 3 shipped seeds load successfully and validate against the
  PropertyPattern v0.2 schema
- Library lookup by id, category filter, summary view
- Domain-neutrality: no FableNet-specific identifiers leak into seed body
  (slot names / formula template / title)
- Duplicate detection
- Custom-directory load
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tm.artifacts import PropertyPatternBody
from tm.patterns import SEED_ROOT, PatternLibrary, load_seed_patterns
from tm.patterns.library import _load_pattern_body


EXPECTED_SEED_IDS = {
    "safety.no_x_amplifies_y",
    "liveness.eventually_x_holds",
    "fairness.bounded_x_across_actors",
}


# ─── Seed library content ─────────────────────────────────────────


class TestSeedLibraryContent:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()

    def test_all_three_seeds_present(self) -> None:
        assert set(self.lib.ids()) == EXPECTED_SEED_IDS

    def test_seed_count_is_three(self) -> None:
        assert len(self.lib) == 3

    def test_categories_one_per_seed(self) -> None:
        cats = {e.category for e in self.lib.entries()}
        assert cats == {"safety", "liveness", "fairness"}

    def test_each_seed_has_at_least_one_slot(self) -> None:
        for entry in self.lib.entries():
            assert len(entry.body.slots) >= 1, f"{entry.pattern_id} has no slots"

    def test_each_seed_has_non_empty_formula_template(self) -> None:
        for entry in self.lib.entries():
            assert entry.body.formula_template.strip(), entry.pattern_id

    def test_each_seed_has_counterexamples(self) -> None:
        for entry in self.lib.entries():
            assert len(entry.body.counterexamples) >= 1, entry.pattern_id

    def test_seeds_are_property_pattern_body_instances(self) -> None:
        for entry in self.lib.entries():
            assert isinstance(entry.body, PropertyPatternBody)

    def test_summary_view(self) -> None:
        summary = self.lib.summary()
        assert len(summary) == 3
        for row in summary:
            assert set(row.keys()) == {"pattern_id", "category", "title", "slots"}


class TestSeedDomainNeutrality:
    """**Critical**: seeds must not reference any FableNet-specific names.

    The seeds may *mention* fablenet-control as one example domain in the
    file-level comments and in ``metadata.abstracts`` (provenance), but
    must not bake FableNet types into slot names, formula templates, or
    titles.
    """

    def setup_method(self) -> None:
        self.lib = load_seed_patterns()

    def test_no_fablenet_in_pattern_ids(self) -> None:
        for pid in self.lib.ids():
            assert "fablenet" not in pid.lower()
            assert "sybil" not in pid.lower()
            assert "fnet" not in pid.lower()

    def test_no_fablenet_in_titles(self) -> None:
        for entry in self.lib.entries():
            t = entry.body.title.lower()
            assert "fablenet" not in t
            assert "sybil" not in t

    def test_no_fablenet_in_formula_templates(self) -> None:
        for entry in self.lib.entries():
            t = entry.body.formula_template.lower()
            assert "fablenet" not in t
            assert "sybil" not in t
            assert "burst" not in t
            assert "quarantine" not in t

    def test_no_fablenet_in_slot_names(self) -> None:
        for entry in self.lib.entries():
            for slot in entry.body.slots:
                n = slot.name.lower()
                assert "fablenet" not in n
                assert "sybil" not in n


# ─── PatternLibrary API ───────────────────────────────────────────


class TestPatternLibraryAPI:
    def test_get_by_id(self) -> None:
        lib = load_seed_patterns()
        entry = lib.get("safety.no_x_amplifies_y")
        assert entry.pattern_id == "safety.no_x_amplifies_y"
        assert entry.category == "safety"

    def test_get_unknown_raises_with_available_list(self) -> None:
        lib = load_seed_patterns()
        with pytest.raises(KeyError, match="not found"):
            lib.get("nope.unknown")
        # The exception message should help the user discover available ids
        try:
            lib.get("nope")
        except KeyError as exc:
            assert "available" in str(exc)
            assert "safety.no_x_amplifies_y" in str(exc)

    def test_contains_operator(self) -> None:
        lib = load_seed_patterns()
        assert "safety.no_x_amplifies_y" in lib
        assert "nope" not in lib

    def test_filter_by_category(self) -> None:
        lib = load_seed_patterns()
        safety = lib.filter_by_category("safety")
        assert len(safety) == 1
        assert safety[0].pattern_id == "safety.no_x_amplifies_y"

    def test_filter_unknown_category_returns_empty(self) -> None:
        lib = load_seed_patterns()
        assert lib.filter_by_category("invariance") == []

    def test_ids_returned_sorted(self) -> None:
        lib = load_seed_patterns()
        ids = lib.ids()
        assert ids == sorted(ids)

    def test_entries_iteration_order_matches_ids(self) -> None:
        lib = load_seed_patterns()
        assert [e.pattern_id for e in lib.entries()] == lib.ids()


class TestPatternLibraryDuplicateDetection:
    def test_duplicate_id_rejected(self, tmp_path: Path) -> None:
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        seed = textwrap.dedent(
            """
            pattern_id: dup.example
            category: safety
            title: A
            formula_template: "AG NOT has(x)"
            slots:
              - name: dummy
                type: ctl_predicate
                required: true
            """
        ).strip()
        a.write_text(seed, encoding="utf-8")
        b.write_text(seed.replace("title: A", "title: B"), encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate pattern_id"):
            PatternLibrary.from_directory(tmp_path)


class TestPatternLibraryCustomDirectory:
    def test_load_from_arbitrary_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "x.yaml"
        f.write_text(
            textwrap.dedent(
                """
                pattern_id: custom.pat
                category: liveness
                title: Custom
                formula_template: "EF has(x)"
                slots:
                  - name: dummy
                    type: ctl_predicate
                    required: false
                """
            ).strip(),
            encoding="utf-8",
        )
        lib = PatternLibrary.from_directory(tmp_path)
        assert lib.ids() == ["custom.pat"]

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            PatternLibrary.from_directory(tmp_path / "nope")

    def test_seed_root_is_default(self) -> None:
        lib = load_seed_patterns()
        # Equivalent invocations should produce identical contents
        lib_explicit = PatternLibrary.from_directory(SEED_ROOT)
        assert lib.ids() == lib_explicit.ids()


class TestPatternBodyLoaderValidation:
    def test_invalid_body_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("not_a_mapping_at_all\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            _load_pattern_body(f)

    def test_missing_required_field_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(
            textwrap.dedent(
                """
                pattern_id: bad.missing_template
                category: safety
                title: Bad
                slots:
                  - name: x
                    type: ctl_predicate
                """
            ).strip(),
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            _load_pattern_body(f)


# ─── PatternEntry dataclass ───────────────────────────────────────


class TestPatternEntry:
    def test_path_recorded(self) -> None:
        lib = load_seed_patterns()
        entry = lib.get("safety.no_x_amplifies_y")
        assert entry.path.exists()
        assert entry.path.name == "no_x_amplifies_y.yaml"

    def test_entry_is_frozen(self) -> None:
        lib = load_seed_patterns()
        entry = lib.get("safety.no_x_amplifies_y")
        with pytest.raises(Exception):
            entry.path = Path("/tmp/other")
