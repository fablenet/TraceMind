"""Accountable sign-off / seal closure invariant (Stage 7-2.8, §4c).

``status=sealed`` ⟺ a human ``sign_off`` exists ∧ the consistency gate's hard
checks all pass (5W1H seal-mode complete ∧ CTL verify ∧ no exact duplicate) ∧
every required dimension is closed (``resolved`` structurally, or ``waived`` /
``dynamic`` in dispositions). The closure is computed server-side from a fresh
seal-mode recompute, never asserted by the caller. API + CLI offer the same
``seal`` action over the shared store (Parity Rule).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tm.artifacts import ArtifactStatus, verify
from tm.cli.intent_chat import register_session_commands
from tm.intent.completeness import Dimension
from tm.intent.session import (
    GateFacts,
    SessionTransitionError,
    advance,
    embed_completeness,
    gate_facts_for,
    new_session,
    seal,
    verify_journal,
)
from tm.intent.session_store import SessionStore, session_body_to_raw
from tm.intent.uncertainty import Disposition, DispositionKind
from tm.server.routes_sessions import create_sessions_router


_COMPLETE_INTENT = {
    "intent_id": "intent.seal",
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


def _write_intent(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _at_accept(tmp_path: Path, intent: dict = _COMPLETE_INTENT):
    """Drive a session deterministically to the ``accept`` step."""
    s = new_session("session.seal", "intent.seal")
    for _ in range(3):  # check_5w1h, propose, refine
        s = advance(s, GateFacts(), role="agent")
    intent_path = _write_intent(tmp_path, intent)
    s = embed_completeness(s, intent_path=intent_path, profile="base")
    s = advance(s, gate_facts_for(s), role="agent")  # → verify
    s = advance(s, GateFacts(verify_passed=True), role="human")  # → accept
    assert s.current_step == "accept"
    return s, intent_path


# ─── happy path ─────────────────────────────────────────────────────


def test_seal_success_builds_signoff(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    sealed = seal(
        s,
        signer="human:alice",
        intent_path=intent_path,
        profile="base",
        verify_passed=True,
        scope=["intent.seal"],
        signed_at="2026-06-13T00:00:00Z",
    )
    assert sealed.status == "sealed" and sealed.current_step == "sealed"
    so = sealed.sign_off
    assert so is not None and so.signer == "human:alice"
    assert so.completeness_snapshot["mode"] == "seal"
    assert so.completeness_snapshot["profile"] == "base"
    assert so.gate_report_hash and so.sign_hash
    assert verify_journal(sealed) == []


def test_sealed_session_passes_artifact_verify(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    sealed = seal(s, signer="human:alice", intent_path=intent_path, verify_passed=True)
    store_raw = session_body_to_raw(sealed)
    from tm.artifacts import Artifact, ArtifactEnvelope, ArtifactType
    from tm.artifacts.models import IntentSessionBody

    env = ArtifactEnvelope(
        artifact_id=sealed.session_id,
        status=ArtifactStatus.CANDIDATE,
        artifact_type=ArtifactType.INTENT_SESSION,
        version="v0",
        created_by="t",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )
    accepted, report = verify(Artifact(envelope=env, body=IntentSessionBody.from_mapping(store_raw), body_raw=store_raw))
    assert report.errors == [], report.errors
    assert accepted is not None


# ─── refusal paths (closure invariant) ──────────────────────────────


def test_seal_without_signer_refused(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    with pytest.raises(SessionTransitionError):
        seal(s, signer="  ", intent_path=intent_path, verify_passed=True)


def test_seal_not_at_accept_refused(tmp_path: Path):
    s = new_session("session.seal", "intent.seal")
    intent_path = _write_intent(tmp_path, _COMPLETE_INTENT)
    with pytest.raises(SessionTransitionError, match="accept"):
        seal(s, signer="human:alice", intent_path=intent_path, verify_passed=True)


def test_seal_without_verify_refused(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    with pytest.raises(SessionTransitionError) as exc:
        seal(s, signer="human:alice", intent_path=intent_path, verify_passed=False)
    assert exc.value.requirement == "human_signoff_and_all_dims_closed"


# A ``What``-partial intent (no inputs/outputs): tolerated through design (the
# error dim is only a warning while partial), but seal-mode requires it closed.
_PARTIAL_WHAT = {**_COMPLETE_INTENT, "inputs": [], "outputs": []}


def test_seal_incomplete_intent_refused(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path, _PARTIAL_WHAT)
    with pytest.raises(SessionTransitionError, match="closure invariant"):
        seal(s, signer="human:alice", intent_path=intent_path, verify_passed=True)


# ─── uncertainty closure unblocks seal (waived / dynamic) ───────────


def test_waived_disposition_closes_and_seals(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path, _PARTIAL_WHAT)
    sealed = seal(
        s,
        signer="human:alice",
        intent_path=intent_path,
        verify_passed=True,
        dispositions={
            Dimension.WHAT: Disposition(
                kind=DispositionKind.WAIVED,
                rationale="inputs/outputs finalized out-of-band this cycle",
                signer="human:alice",
            )
        },
    )
    assert sealed.status == "sealed"
    assert sealed.sign_off.dispositions["what"]["kind"] == "waived"


def test_dynamic_disposition_with_registered_resolver_seals(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path, _PARTIAL_WHAT)
    sealed = seal(
        s,
        signer="human:alice",
        intent_path=intent_path,
        verify_passed=True,
        dispositions={
            Dimension.WHAT: Disposition(
                kind=DispositionKind.DYNAMIC,
                resolver_ref="constant",
                schema={"value": "content"},
            )
        },
    )
    assert sealed.sign_off.dispositions["what"]["kind"] == "dynamic"


def test_unregistered_dynamic_resolver_refused(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path, _PARTIAL_WHAT)
    with pytest.raises(SessionTransitionError, match="closure invariant"):
        seal(
            s,
            signer="human:alice",
            intent_path=intent_path,
            verify_passed=True,
            dispositions={Dimension.WHAT: Disposition(kind=DispositionKind.DYNAMIC, resolver_ref="rogue_llm")},
        )


# ─── determinism + sealed is read-only ──────────────────────────────


def test_seal_is_deterministic(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    kwargs = dict(signer="human:alice", intent_path=intent_path, verify_passed=True, signed_at="2026-06-13T00:00:00Z")
    a = seal(s, **kwargs)
    b = seal(s, **kwargs)
    assert session_body_to_raw(a) == session_body_to_raw(b)


def test_sealed_refuses_reseal(tmp_path: Path):
    s, intent_path = _at_accept(tmp_path)
    sealed = seal(s, signer="human:alice", intent_path=intent_path, verify_passed=True)
    with pytest.raises(SessionTransitionError):
        seal(sealed, signer="human:alice", intent_path=intent_path, verify_passed=True)


# ─── API + CLI parity ───────────────────────────────────────────────


def _app(store_dir: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(create_sessions_router(SessionStore(store_dir)))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _seed_accept_session(store_dir: Path, tmp_path: Path) -> Path:
    s, intent_path = _at_accept(tmp_path)
    SessionStore(store_dir).save(s)
    return intent_path


@pytest.mark.asyncio
async def test_api_seal_success_and_block(tmp_path: Path):
    store_dir = tmp_path / "s"
    intent_path = _seed_accept_session(store_dir, tmp_path)
    app = _app(store_dir)
    async with _client(app) as client:
        blocked = await client.post(
            "/api/v1/sessions/session.seal:seal",
            json={"signer": "human:alice", "intent_path": str(intent_path), "verify_passed": False},
        )
        assert blocked.status_code == 409

        ok = await client.post(
            "/api/v1/sessions/session.seal:seal",
            json={"signer": "human:alice", "intent_path": str(intent_path), "verify_passed": True},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "sealed"


def _run_cli(argv: list[str]):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    ip = sub.add_parser("intent")
    isub = ip.add_subparsers(dest="intent_cmd")
    isub.required = True
    register_session_commands(isub)
    args = parser.parse_args(argv)
    return args.func(args)


def test_cli_seal_matches_store(tmp_path: Path, capsys):
    store_dir = tmp_path / "s"
    intent_path = _seed_accept_session(store_dir, tmp_path)
    rc = _run_cli(
        [
            "intent", "session", "seal", "session.seal",
            "--signer", "human:alice", "--intent", str(intent_path),
            "--verify-passed", "--sessions-dir", str(store_dir),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "sealed"
    body = SessionStore(store_dir).load("session.seal")
    assert body.sign_off.signer == "human:alice"
