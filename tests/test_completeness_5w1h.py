"""Stage 7-0.2 — 5W1H profile model + loader tests (deterministic, no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

import json

from tm.intent.completeness import (
    ALL_DIMENSIONS,
    Dimension,
    Profile,
    Severity,
    compute_5w1h_completeness,
    load_profile,
)


def test_base_profile_severities() -> None:
    profile = load_profile("base")
    assert isinstance(profile, Profile)
    assert profile.profile_id == "base"
    assert profile.domain is None
    # spec §2 base severities
    assert profile.severity(Dimension.WHO) is Severity.ERROR
    assert profile.severity(Dimension.WHY) is Severity.ERROR
    assert profile.severity(Dimension.WHAT) is Severity.ERROR
    assert profile.severity(Dimension.HOW) is Severity.ERROR
    assert profile.severity(Dimension.WHEN) is Severity.WARN
    assert profile.severity(Dimension.WHERE) is Severity.WARN
    # every dimension resolves to a severity
    assert all(profile.severity(d) in Severity for d in ALL_DIMENSIONS)


def test_base_has_when_where_hints() -> None:
    profile = load_profile("base")
    assert profile.hint(Dimension.WHEN)
    assert profile.hint(Dimension.WHERE)
    assert profile.hint(Dimension.WHO) is None


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_extends_overrides_and_unions(tmp_path: Path) -> None:
    child = _write(
        tmp_path / "fablenet.yaml",
        """
profile_id: fablenet.anonymity.v1
domain: fablenet
extends: base
severity_overrides:
  where: error
  when: error
required_slots:
  - anonymity_scope
vocabulary_hints:
  where: "declare AgentNetwork or metadata.domain=fablenet"
""",
    )
    profile = load_profile(child)
    assert profile.profile_id == "fablenet.anonymity.v1"
    assert profile.domain == "fablenet"
    # overrides applied
    assert profile.severity(Dimension.WHERE) is Severity.ERROR
    assert profile.severity(Dimension.WHEN) is Severity.ERROR
    # inherited untouched
    assert profile.severity(Dimension.WHO) is Severity.ERROR
    # required_slots from child
    assert profile.required_slots == ("anonymity_scope",)
    # hints: child overrides where, inherits when from base
    assert "fablenet" in (profile.hint(Dimension.WHERE) or "")
    assert profile.hint(Dimension.WHEN)  # inherited from base


def test_off_severity_supported(tmp_path: Path) -> None:
    child = _write(
        tmp_path / "loose.yaml",
        """
profile_id: loose.v1
extends: base
severity_overrides:
  when: off
  where: off
""",
    )
    profile = load_profile(child)
    assert profile.severity(Dimension.WHEN) is Severity.OFF
    assert profile.severity(Dimension.WHERE) is Severity.OFF


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="profile not found"):
        load_profile("does-not-exist-domain")


def test_unknown_dimension_raises(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "bad.yaml",
        """
profile_id: bad.v1
extends: base
severity_overrides:
  whoops: error
""",
    )
    with pytest.raises(ValueError, match="unknown 5W1H dimension"):
        load_profile(bad)


# ─── compute_5w1h_completeness (Task 7-0.3) ──────────────────────


def _intent(**overrides) -> dict:
    base = {
        "intent_id": "intent.demo",
        "title": "demo",
        "context": "we operate an anonymous feed",
        "goal": "fairly disseminate viewpoints",
        "non_goals": [],
        "actors": ["reader", "author"],
        "inputs": ["content"],
        "outputs": ["ranked_feed"],
        "constraints": [],
        "success_metrics": [],
        "risks": [],
        "assumptions": [],
        "trace_links": {"parent_intent": None, "related_intents": []},
        "property_pattern_refs": ["fairness.bounded_x_across_actors"],
        "slot_fills": {},
    }
    base.update(overrides)
    return base


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_complete_intent_passes(tmp_path: Path) -> None:
    # Who/Why/What/How satisfied; When/Where only warn under base.
    p = _write_json(tmp_path / "intent.json", _intent())
    outcome = compute_5w1h_completeness(intent_path=p, profile="base")
    assert outcome.exit_code == 0  # no error-severity dim unmet
    dims = outcome.report["dimensions"]
    assert dims["who"]["status"] == "satisfied"
    assert dims["why"]["status"] == "satisfied"
    assert dims["what"]["status"] == "satisfied"
    assert dims["how"]["status"] == "satisfied"
    # When/Where missing but only warn under base → no exit failure
    assert dims["when"]["status"] == "missing"
    assert dims["where"]["status"] == "missing"
    assert outcome.report["summary"]["warnings"] == 2


def test_missing_error_dimension_fails(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "intent.json", _intent(actors=[]))
    outcome = compute_5w1h_completeness(intent_path=p, profile="base")
    assert outcome.exit_code == 1
    assert "who" in outcome.report["missing_dimensions"]
    assert outcome.report["dimensions"]["who"]["severity"] == "error"


def test_partial_why_blocks_in_seal_tolerated_in_design(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "intent.json", _intent(context=""))
    # seal: a partial error-dim blocks (strict closure)
    sealed = compute_5w1h_completeness(intent_path=p, profile="base", mode="seal")
    assert sealed.exit_code == 1
    assert sealed.report["dimensions"]["why"]["status"] == "partial"
    assert sealed.report["mode"] == "seal"
    # design: same partial is tolerated → warning, not blocking
    designed = compute_5w1h_completeness(intent_path=p, profile="base", mode="design")
    assert designed.exit_code == 0
    assert designed.report["dimensions"]["why"]["status"] == "partial"
    assert designed.report["summary"]["warnings"] >= 1


def test_when_satisfied_via_slot(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "intent.json",
        _intent(slot_fills={"fairness.bounded_x_across_actors": {"when_window": "1h"}}),
    )
    outcome = compute_5w1h_completeness(intent_path=p, profile="base")
    assert outcome.report["dimensions"]["when"]["status"] == "satisfied"


def test_where_satisfied_via_network(tmp_path: Path) -> None:
    intent = _write_json(tmp_path / "intent.json", _intent())
    network = _write_json(tmp_path / "net.json", {"network_id": "network.demo"})
    outcome = compute_5w1h_completeness(intent_path=intent, network_path=network, profile="base")
    assert outcome.report["dimensions"]["where"]["status"] == "satisfied"


def test_profile_off_makes_not_applicable(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "loose.yaml",
        """
profile_id: loose.v1
extends: base
severity_overrides:
  when: off
  where: off
""",
    )
    p = _write_json(tmp_path / "intent.json", _intent())
    outcome = compute_5w1h_completeness(intent_path=p, profile=prof)
    assert outcome.report["dimensions"]["when"]["status"] == "not_applicable"
    assert outcome.report["summary"]["not_applicable"] == 2
    assert outcome.report["summary"]["warnings"] == 0


def test_seal_profile_promotes_where_to_error(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "fab.yaml",
        """
profile_id: fablenet.v1
extends: base
severity_overrides:
  where: error
  when: error
""",
    )
    p = _write_json(tmp_path / "intent.json", _intent())  # no When/Where evidence
    outcome = compute_5w1h_completeness(intent_path=p, profile=prof)
    assert outcome.exit_code == 1  # When/Where now error-severity


def test_report_is_deterministic(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "intent.json", _intent())
    a = compute_5w1h_completeness(intent_path=p, profile="base").report
    b = compute_5w1h_completeness(intent_path=p, profile="base").report
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ─── Cross-artifact + required_slots (Task 7-0.4) ────────────────


def test_when_satisfied_via_seed_liveness_pattern(tmp_path: Path) -> None:
    # Referencing a liveness PropertyPattern is When evidence (default seeds).
    p = _write_json(
        tmp_path / "intent.json",
        _intent(property_pattern_refs=["liveness.eventually_x_holds"]),
    )
    outcome = compute_5w1h_completeness(intent_path=p, profile="base")
    when = outcome.report["dimensions"]["when"]
    assert when["status"] == "satisfied"
    assert when["evidence"] == ["pattern:liveness.eventually_x_holds:liveness"]


def test_when_not_satisfied_via_non_liveness_pattern(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "intent.json",
        _intent(property_pattern_refs=["fairness.bounded_x_across_actors"]),
    )
    outcome = compute_5w1h_completeness(intent_path=p, profile="base")
    assert outcome.report["dimensions"]["when"]["status"] == "missing"


def test_when_via_downstream_patterns_dir(tmp_path: Path) -> None:
    pdir = tmp_path / "patterns" / "liveness"
    pdir.mkdir(parents=True)
    (pdir / "custom_live.yaml").write_text(
        """
pattern_id: custom.live
category: liveness
title: custom liveness
formula_template: "EF {g}"
slots:
  - name: g
    type: ctl_predicate
    description: target predicate
    required: true
""",
        encoding="utf-8",
    )
    p = _write_json(tmp_path / "intent.json", _intent(property_pattern_refs=["custom.live"]))
    outcome = compute_5w1h_completeness(
        intent_path=p, profile="base", patterns_dir=tmp_path / "patterns"
    )
    assert outcome.report["dimensions"]["when"]["status"] == "satisfied"


def test_required_slots_degrades_what(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "fab.yaml",
        """
profile_id: fab.v1
extends: base
required_slots:
  - anonymity_scope
""",
    )
    # missing the required slot → What (default target) degraded to partial
    p = _write_json(tmp_path / "intent.json", _intent())
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal")
    assert out.report["missing_required_slots"] == ["anonymity_scope"]
    what = out.report["dimensions"]["what"]
    assert what["status"] == "partial"
    assert "anonymity_scope" in (what["missing_reason"] or "")
    assert out.exit_code == 1  # What is error-severity, seal blocks

    # provide the slot → satisfied again
    ok = _write_json(
        tmp_path / "ok.json",
        _intent(slot_fills={"some.pattern": {"anonymity_scope": "global"}}),
    )
    out2 = compute_5w1h_completeness(intent_path=ok, profile=prof, mode="seal")
    assert out2.report["missing_required_slots"] == []
    assert out2.report["dimensions"]["what"]["status"] == "satisfied"


def test_required_slots_dimension_override(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "where_slot.yaml",
        """
profile_id: where_slot.v1
extends: base
required_slots:
  - region
required_slots_dimension: where
""",
    )
    loaded = load_profile(prof)
    assert loaded.required_slots_dimension is Dimension.WHERE
    p = _write_json(tmp_path / "intent.json", _intent())
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="design")
    where = out.report["dimensions"]["where"]
    assert "region" in (where["missing_reason"] or "")


# ─── Two-phase judgment (Task 7-0.10) ────────────────────────────


def test_design_heuristic_downgrades_missing_to_partial(tmp_path: Path) -> None:
    # When is error under this profile; intent has NO structured When evidence,
    # but its prose mentions a timing trigger → design downgrades to partial.
    prof = _write(
        tmp_path / "timed.yaml",
        """
profile_id: timed.v1
extends: base
severity_overrides:
  when: error
""",
    )
    body = _intent(goal="periodic reconciliation every 5m to keep the feed fresh")
    p = _write_json(tmp_path / "intent.json", body)

    designed = compute_5w1h_completeness(intent_path=p, profile=prof, mode="design")
    when = designed.report["dimensions"]["when"]
    assert when["status"] == "partial"
    assert any(e.startswith("heuristic:") for e in when["evidence"])
    assert designed.exit_code == 0  # tolerated partial does not block design

    # seal: heuristic OFF → structurally missing → blocks
    sealed = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal")
    assert sealed.report["dimensions"]["when"]["status"] == "missing"
    assert sealed.exit_code == 1


def test_design_no_prose_hint_stays_missing(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "timed.yaml",
        """
profile_id: timed.v1
extends: base
severity_overrides:
  when: error
""",
    )
    # prose has no timing vocabulary at all
    body = _intent(context="anonymous platform", goal="rank viewpoints")
    p = _write_json(tmp_path / "intent.json", body)
    designed = compute_5w1h_completeness(intent_path=p, profile=prof, mode="design")
    assert designed.report["dimensions"]["when"]["status"] == "missing"
    assert designed.exit_code == 1  # nothing to tolerate → still blocks


def test_profile_heuristic_keywords_extend_lexicon(tmp_path: Path) -> None:
    prof = _write(
        tmp_path / "custom.yaml",
        """
profile_id: custom.v1
extends: base
severity_overrides:
  where: error
heuristic_keywords:
  where:
    - shard
""",
    )
    body = _intent(goal="route writes to the correct shard")
    p = _write_json(tmp_path / "intent.json", body)
    designed = compute_5w1h_completeness(intent_path=p, profile=prof, mode="design")
    where = designed.report["dimensions"]["where"]
    assert where["status"] == "partial"
    assert where["evidence"] == ["heuristic:shard"]


def test_invalid_mode_raises(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "intent.json", _intent())
    with pytest.raises(ValueError):
        compute_5w1h_completeness(intent_path=p, profile="base", mode="bogus")


def test_extends_cycle_detected(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.yaml",
        """
profile_id: a
extends: b
""",
    )
    _write(
        tmp_path / "b.yaml",
        """
profile_id: b
extends: a
""",
    )
    # extends resolves by name under PROFILES_DIR, so point both at tmp via path
    # by loading 'a' which extends 'b' which extends 'a' — cycle guard trips.
    with pytest.raises(ValueError, match="cycle"):
        load_profile(tmp_path / "a.yaml")
