"""Stage 7-0.11 — uncertainty closure at seal (deterministic, no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tm.intent.completeness import compute_5w1h_completeness
from tm.intent.uncertainty import (
    Disposition,
    DispositionKind,
    is_registered_resolver,
    load_dispositions,
    parse_disposition,
    register_resolver,
    registered_resolvers,
    validate_disposition,
)


def _intent(**overrides) -> dict:
    base = {
        "intent_id": "intent.demo",
        "title": "demo",
        "context": "anonymous platform",
        "goal": "rank viewpoints",
        "non_goals": [],
        "actors": ["reader"],
        "inputs": ["content"],
        "outputs": ["feed"],
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


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seal_profile(tmp_path: Path) -> Path:
    p = tmp_path / "seal.yaml"
    p.write_text(
        """
profile_id: seal.v1
extends: base
severity_overrides:
  when: error
  where: error
""",
        encoding="utf-8",
    )
    return p


# ─── registry / validation units ─────────────────────────────────


def test_constant_resolver_is_registered() -> None:
    assert is_registered_resolver("constant")
    assert "constant" in registered_resolvers()
    assert not is_registered_resolver("does-not-exist")


def test_waived_requires_rationale_and_signer() -> None:
    bad = Disposition(kind=DispositionKind.WAIVED)
    errs = validate_disposition(bad)
    assert any("rationale" in e for e in errs)
    assert any("signer" in e for e in errs)
    good = Disposition(kind=DispositionKind.WAIVED, rationale="pilot is single-tenant", signer="alice")
    assert validate_disposition(good) == []


def test_dynamic_requires_registered_resolver() -> None:
    unknown = Disposition(kind=DispositionKind.DYNAMIC, resolver_ref="nope")
    assert any("not a registered" in e for e in validate_disposition(unknown))
    ok = Disposition(kind=DispositionKind.DYNAMIC, resolver_ref="constant", schema={"value": "5m"})
    assert validate_disposition(ok) == []


def test_parse_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown disposition kind"):
        parse_disposition({"kind": "magic"})


# ─── seal closure end-to-end ─────────────────────────────────────


def test_seal_blocks_without_disposition(tmp_path: Path) -> None:
    prof = _seal_profile(tmp_path)
    p = _write_json(tmp_path / "intent.json", _intent())
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal")
    assert out.exit_code == 1
    assert out.report["sealed"] is False


def test_seal_closed_by_waived_and_dynamic(tmp_path: Path) -> None:
    prof = _seal_profile(tmp_path)
    p = _write_json(tmp_path / "intent.json", _intent())
    dispositions = {
        "where": Disposition(
            kind=DispositionKind.WAIVED,
            rationale="single-tenant pilot; scope fixed by deployment",
            signer="alice",
            uncertainty_registry_ref="UR-2026-013",
        ),
        "when": Disposition(
            kind=DispositionKind.DYNAMIC, resolver_ref="constant", schema={"value": "5m"}
        ),
    }
    from tm.intent.completeness import Dimension

    dispo = {Dimension(k): v for k, v in dispositions.items()}
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal", dispositions=dispo)
    assert out.exit_code == 0
    assert out.report["sealed"] is True
    assert out.report["summary"]["closed_by_disposition"] == 2
    assert out.report["dimensions"]["where"]["closed"] is True
    assert out.report["dimensions"]["when"]["disposition"]["resolver_ref"] == "constant"
    assert out.report["closure"] == {"when": "dynamic", "where": "waived"}


def test_seal_invalid_waiver_does_not_close(tmp_path: Path) -> None:
    prof = _seal_profile(tmp_path)
    p = _write_json(tmp_path / "intent.json", _intent())
    from tm.intent.completeness import Dimension

    dispo = {Dimension.WHERE: Disposition(kind=DispositionKind.WAIVED)}  # no rationale/signer
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal", dispositions=dispo)
    assert out.exit_code == 1  # where still unmet, waiver invalid
    where = out.report["dimensions"]["where"]
    assert where["closed"] is False
    assert "rationale" in (where["closure_reason"] or "")


def test_seal_resolved_claim_rejected_when_unmet(tmp_path: Path) -> None:
    prof = _seal_profile(tmp_path)
    p = _write_json(tmp_path / "intent.json", _intent())
    from tm.intent.completeness import Dimension

    dispo = {Dimension.WHEN: Disposition(kind=DispositionKind.RESOLVED)}
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal", dispositions=dispo)
    assert out.exit_code == 1
    when = out.report["dimensions"]["when"]
    assert when["closed"] is False
    assert "resolved" in (when["closure_reason"] or "")


def test_seal_resolved_ok_when_structured(tmp_path: Path) -> None:
    prof = _seal_profile(tmp_path)
    # actually provide structured When evidence (slot) + Where via network marker slot
    body = _intent(
        slot_fills={"p": {"when_window": "5m", "where_scope": "ns/feed"}}
    )
    p = _write_json(tmp_path / "intent.json", body)
    from tm.intent.completeness import Dimension

    dispo = {Dimension.WHEN: Disposition(kind=DispositionKind.RESOLVED)}
    out = compute_5w1h_completeness(intent_path=p, profile=prof, mode="seal", dispositions=dispo)
    assert out.report["dimensions"]["when"]["status"] == "satisfied"
    assert out.report["dimensions"]["when"]["closed"] is True
    assert out.exit_code == 0


def test_load_dispositions_from_file(tmp_path: Path) -> None:
    f = tmp_path / "dispo.yaml"
    f.write_text(
        """
where:
  kind: waived
  rationale: fixed by deployment
  signer: bob
when:
  kind: dynamic
  resolver_ref: constant
  schema:
    value: 5m
""",
        encoding="utf-8",
    )
    loaded = load_dispositions(f)
    from tm.intent.completeness import Dimension

    assert loaded[Dimension.WHERE].kind is DispositionKind.WAIVED
    assert loaded[Dimension.WHEN].resolver_ref == "constant"


def test_register_resolver_extends_registry() -> None:
    @register_resolver("test.echo")
    def _echo(schema):  # noqa: ANN001
        return schema.get("value")

    assert is_registered_resolver("test.echo")
    assert validate_disposition(
        Disposition(kind=DispositionKind.DYNAMIC, resolver_ref="test.echo")
    ) == []
