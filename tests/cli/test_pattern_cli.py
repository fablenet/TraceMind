"""Tests for ``tm pattern`` CLI subcommands (Stage 5-3 task 3.2).

Covers list / show / instantiate with both table and JSON output, custom
``--library`` directory, and error paths (unknown pattern_id, malformed
``--slot``, missing required slots).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tm", *args],
        capture_output=True,
        text=True,
    )


# ─── list ─────────────────────────────────────────────────────────


class TestPatternList:
    def test_list_default_table(self) -> None:
        result = _run(["pattern", "list"])
        assert result.returncode == 0, result.stderr
        assert "safety.no_x_amplifies_y" in result.stdout
        assert "liveness.eventually_x_holds" in result.stdout
        assert "fairness.bounded_x_across_actors" in result.stdout
        assert "category" in result.stdout

    def test_list_json(self) -> None:
        result = _run(["pattern", "list", "--json"])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "patterns" in payload
        pattern_ids = [p["pattern_id"] for p in payload["patterns"]]
        assert set(pattern_ids) == {
            "safety.no_x_amplifies_y",
            "liveness.eventually_x_holds",
            "fairness.bounded_x_across_actors",
        }

    def test_list_filter_by_category(self) -> None:
        result = _run(["pattern", "list", "--category", "safety", "--json"])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert len(payload["patterns"]) == 1
        assert payload["patterns"][0]["category"] == "safety"

    def test_list_invalid_category_rejected(self) -> None:
        result = _run(["pattern", "list", "--category", "rogue"])
        assert result.returncode != 0
        assert "invalid choice" in result.stderr or "choose from" in result.stderr

    def test_list_empty_custom_library(self, tmp_path: Path) -> None:
        result = _run(["pattern", "--library", str(tmp_path), "list"])
        # Empty directory is a valid library with 0 patterns
        assert result.returncode == 0, result.stderr
        assert "(no patterns)" in result.stdout

    def test_list_missing_library_directory(self, tmp_path: Path) -> None:
        result = _run(["pattern", "--library", str(tmp_path / "nope"), "list"])
        assert result.returncode == 1
        assert "not found" in result.stderr


# ─── show ─────────────────────────────────────────────────────────


class TestPatternShow:
    def test_show_full_body_yaml(self) -> None:
        result = _run(["pattern", "show", "safety.no_x_amplifies_y"])
        assert result.returncode == 0, result.stderr
        assert "pattern_id: safety.no_x_amplifies_y" in result.stdout
        assert "formula_template:" in result.stdout
        assert "slots:" in result.stdout
        assert "counterexamples:" in result.stdout

    def test_show_json(self) -> None:
        result = _run(["pattern", "show", "liveness.eventually_x_holds", "--json"])
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["pattern_id"] == "liveness.eventually_x_holds"
        assert body["category"] == "liveness"
        assert isinstance(body["slots"], list)

    def test_show_unknown_pattern(self) -> None:
        result = _run(["pattern", "show", "nope.unknown"])
        assert result.returncode == 1
        assert "not found" in result.stderr
        # Helpful "available" list should be shown
        assert "safety.no_x_amplifies_y" in result.stderr

    def test_show_includes_source_path(self) -> None:
        result = _run(["pattern", "show", "safety.no_x_amplifies_y", "--json"])
        assert result.returncode == 0
        body = json.loads(result.stdout)
        assert body["_source_path"].endswith("no_x_amplifies_y.yaml")


# ─── instantiate ──────────────────────────────────────────────────


class TestPatternInstantiate:
    def test_instantiate_stdout_yaml(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "safety.no_x_amplifies_y",
                "--slot",
                "forbidden_predicate=has(quarantined)",
                "--slot",
                "required_predicate=has(burst_detected)",
            ]
        )
        assert result.returncode == 0, result.stderr
        assert "AG (NOT has(quarantined) OR has(burst_detected))" in result.stdout
        assert "forbidden_predicate" in result.stdout

    def test_instantiate_stdout_json(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(content_discoverable)",
                "--json",
            ]
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["resolved_formula"] == "EF has(content_discoverable)"
        assert out["category"] == "liveness"

    def test_instantiate_writes_yaml_file(self, tmp_path: Path) -> None:
        target = tmp_path / "instance.yaml"
        result = _run(
            [
                "pattern",
                "instantiate",
                "fairness.bounded_x_across_actors",
                "--slot",
                "enforcement_predicate=has(quarantined)",
                "--slot",
                "mediation_predicate=done(human_review)",
                "--output",
                str(target),
            ]
        )
        assert result.returncode == 0, result.stderr
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "AG (NOT has(quarantined) OR EF done(human_review))" in text

    def test_instantiate_writes_json_file(self, tmp_path: Path) -> None:
        target = tmp_path / "instance.json"
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(content_discoverable)",
                "--output",
                str(target),
            ]
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(target.read_text(encoding="utf-8"))
        assert out["resolved_formula"] == "EF has(content_discoverable)"

    def test_instantiate_custom_title(self, tmp_path: Path) -> None:
        target = tmp_path / "instance.yaml"
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(x)",
                "--title",
                "my custom label",
                "--output",
                str(target),
            ]
        )
        assert result.returncode == 0, result.stderr
        assert "my custom label" in target.read_text(encoding="utf-8")

    def test_instantiate_quiet_suppresses_wrote_message(self, tmp_path: Path) -> None:
        target = tmp_path / "instance.yaml"
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(x)",
                "--output",
                str(target),
                "--quiet",
            ]
        )
        assert result.returncode == 0, result.stderr
        assert "wrote" not in result.stderr

    def test_instantiate_no_validate_flag(self) -> None:
        """``--no-validate`` allows formulas that wouldn't parse as CTL."""
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=raw nonsense (",
                "--no-validate",
            ]
        )
        assert result.returncode == 0, result.stderr
        assert "raw nonsense" in result.stdout


class TestPatternInstantiateErrors:
    def test_unknown_pattern_id(self) -> None:
        result = _run(["pattern", "instantiate", "nope.unknown"])
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_missing_required_slot(self) -> None:
        result = _run(["pattern", "instantiate", "liveness.eventually_x_holds"])
        assert result.returncode == 1
        assert "missing required slot" in result.stderr

    def test_unknown_slot(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(x)",
                "--slot",
                "rogue_slot=foo",
            ]
        )
        assert result.returncode == 1
        assert "unknown slot" in result.stderr

    def test_malformed_slot_no_equals(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "bad-no-equals",
            ]
        )
        assert result.returncode == 2
        assert "KEY=VALUE" in result.stderr

    def test_duplicate_slot_rejected(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=has(a)",
                "--slot",
                "goal_predicate=has(b)",
            ]
        )
        assert result.returncode == 2
        assert "multiple times" in result.stderr

    def test_invalid_ctl_in_slot_caught(self) -> None:
        result = _run(
            [
                "pattern",
                "instantiate",
                "liveness.eventually_x_holds",
                "--slot",
                "goal_predicate=!!! bad ctl (",
            ]
        )
        assert result.returncode == 1
        assert "failed CTL parse" in result.stderr


# ─── custom library directory ─────────────────────────────────────


class TestPatternCustomLibrary:
    def test_use_custom_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "custom.yaml"
        f.write_text(
            textwrap.dedent("""
                pattern_id: custom.example
                category: liveness
                title: Custom pattern
                formula_template: "EF has({target})"
                slots:
                  - name: target
                    type: ctl_predicate
                    required: true
                """).strip(),
            encoding="utf-8",
        )
        result = _run(
            [
                "pattern",
                "--library",
                str(tmp_path),
                "list",
                "--json",
            ]
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert len(payload["patterns"]) == 1
        assert payload["patterns"][0]["pattern_id"] == "custom.example"

    def test_instantiate_from_custom_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "custom.yaml"
        f.write_text(
            textwrap.dedent("""
                pattern_id: custom.example
                category: safety
                title: Custom safety
                formula_template: "AG NOT {pred}"
                slots:
                  - name: pred
                    type: ctl_predicate
                    required: true
                """).strip(),
            encoding="utf-8",
        )
        result = _run(
            [
                "pattern",
                "--library",
                str(tmp_path),
                "instantiate",
                "custom.example",
                "--slot",
                "pred=has(x)",
                "--json",
            ]
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["resolved_formula"] == "AG NOT has(x)"
