from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _intent(**overrides: Any) -> dict:
    base = {
        "intent_id": "intent.demo",
        "title": "demo",
        "context": "anonymous feed",
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


def _run(
    intents: Path,
    *,
    profile: str = "base",
    mode: str | None = None,
    dispositions: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable, "-m", "tm", "intents", "check-5w1h",
        "--intents", str(intents), "--profile", profile,
    ]
    if mode is not None:
        cmd += ["--mode", mode]
    if dispositions is not None:
        cmd += ["--dispositions", str(dispositions)]
    return subprocess.run(cmd, cwd=_repo_root(), capture_output=True)


def _report(result: subprocess.CompletedProcess[bytes]) -> Mapping[str, Any]:
    return json.loads(result.stdout.decode("utf-8"))


def test_check_5w1h_complete_exit_0(tmp_path: Path) -> None:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(_intent()), encoding="utf-8")
    result = _run(p)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    report = _report(result)
    assert report["dimensions"]["who"]["status"] == "satisfied"
    assert report["summary"]["errors"] == 0


def test_check_5w1h_missing_actor_exit_1(tmp_path: Path) -> None:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(_intent(actors=[])), encoding="utf-8")
    result = _run(p)
    assert result.returncode == 1, result.stderr.decode("utf-8")
    report = _report(result)
    assert "who" in report["missing_dimensions"]


def test_check_5w1h_output_is_deterministic_bytes(tmp_path: Path) -> None:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(_intent()), encoding="utf-8")
    a = _run(p)
    b = _run(p)
    assert a.returncode == 0, a.stderr.decode("utf-8")
    assert a.stdout == b.stdout


def test_check_5w1h_bad_path_exit_1(tmp_path: Path) -> None:
    result = _run(tmp_path / "nope.json")
    assert result.returncode == 1


def test_check_5w1h_seal_mode_stricter_than_design(tmp_path: Path) -> None:
    # context empty → Why partial (error dim): tolerated in design, blocks in seal.
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(_intent(context="")), encoding="utf-8")

    designed = _run(p, mode="design")
    assert designed.returncode == 0, designed.stderr.decode("utf-8")
    assert _report(designed)["mode"] == "design"

    sealed = _run(p, mode="seal")
    assert sealed.returncode == 1, sealed.stderr.decode("utf-8")
    sealed_report = _report(sealed)
    assert sealed_report["mode"] == "seal"
    assert sealed_report["dimensions"]["why"]["status"] == "partial"


def test_check_5w1h_seal_closed_by_dispositions(tmp_path: Path) -> None:
    prof = tmp_path / "seal.yaml"
    prof.write_text(
        "profile_id: seal.v1\nextends: base\nseverity_overrides:\n  when: error\n  where: error\n",
        encoding="utf-8",
    )
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(_intent()), encoding="utf-8")
    dispo = tmp_path / "dispo.yaml"
    dispo.write_text(
        "where:\n  kind: waived\n  rationale: fixed by deployment\n  signer: bob\n"
        "when:\n  kind: dynamic\n  resolver_ref: constant\n  schema:\n    value: 5m\n",
        encoding="utf-8",
    )
    # without dispositions → blocks
    blocked = _run(p, profile=str(prof), mode="seal")
    assert blocked.returncode == 1
    # with valid dispositions → sealed
    ok = _run(p, profile=str(prof), mode="seal", dispositions=dispo)
    assert ok.returncode == 0, ok.stderr.decode("utf-8")
    report = _report(ok)
    assert report["sealed"] is True
    assert report["summary"]["closed_by_disposition"] == 2
