"""Stage 7-0.5 — domain extensibility.

TraceMind core is domain-neutral: it ships ONLY the `base` profile. Domains
(FableNet, K8s, robotics, …) are downstream/examples that bring their own
profile file. These tests prove a new domain needs ONLY a profile file —
zero changes to `completeness.py` or CTL.
"""

from __future__ import annotations

import json
from pathlib import Path

from tm.intent.completeness import (
    PROFILES_DIR,
    Dimension,
    Severity,
    compute_5w1h_completeness,
    load_profile,
)

_FIXTURE_PROFILES = Path(__file__).resolve().parent / "fixtures" / "intents" / "profiles"


def test_core_ships_only_base_profile() -> None:
    # Core stays domain-neutral: fablenet/k8s/etc MUST NOT leak into core.
    core_profiles = sorted(p.name for p in PROFILES_DIR.glob("*.yaml"))
    assert core_profiles == ["base.yaml"]


def test_downstream_profiles_load_by_path() -> None:
    fab = load_profile(_FIXTURE_PROFILES / "fablenet.yaml")
    assert fab.profile_id == "fablenet.anonymity.v1"
    assert fab.domain == "fablenet"
    assert fab.severity(Dimension.WHERE) is Severity.ERROR
    assert fab.severity(Dimension.WHEN) is Severity.ERROR
    assert fab.severity(Dimension.WHO) is Severity.ERROR  # inherited from base
    assert "anonymity_scope" in fab.required_slots

    k8s = load_profile(_FIXTURE_PROFILES / "k8s.yaml")
    assert k8s.domain == "k8s"
    assert k8s.severity(Dimension.WHERE) is Severity.ERROR
    assert k8s.severity(Dimension.WHEN) is Severity.WARN  # NOT promoted (base default)


def _intent(**overrides) -> dict:
    base = {
        "intent_id": "intent.demo",
        "title": "demo",
        "context": "ctx",
        "goal": "goal",
        "non_goals": [],
        "actors": ["a"],
        "inputs": ["i"],
        "outputs": ["o"],
        "constraints": [],
        "success_metrics": [],
        "risks": [],
        "assumptions": [],
        "trace_links": {"parent_intent": None, "related_intents": []},
        "property_pattern_refs": ["p"],
        "slot_fills": {},
    }
    base.update(overrides)
    return base


def test_new_domain_works_with_only_a_profile_file(tmp_path: Path) -> None:
    """The hard DoD assertion: a domain the core has never seen (robotics)
    works through the SAME public API + new file, with zero core changes."""
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps(_intent()), encoding="utf-8")

    # base: When is warn → no error
    base_out = compute_5w1h_completeness(intent_path=intent, profile="base")
    assert base_out.report["dimensions"]["when"]["severity"] == "warn"
    assert base_out.exit_code == 0

    # robotics: brand-new domain promotes When → error, purely via its file
    robo_out = compute_5w1h_completeness(
        intent_path=intent, profile=_FIXTURE_PROFILES / "robotics.yaml"
    )
    assert robo_out.report["profile"] == "robotics.safety.v1"
    assert robo_out.report["dimensions"]["when"]["severity"] == "error"
    assert robo_out.exit_code == 1  # When now error-severity and unmet


def test_all_three_domains_distinct_no_core_coupling() -> None:
    profiles = {
        load_profile(_FIXTURE_PROFILES / name).domain
        for name in ("fablenet.yaml", "k8s.yaml", "robotics.yaml")
    }
    assert profiles == {"fablenet", "k8s", "robotics"}
