"""Ambiguity clarification: soft warning → question → disposition (Stage 7-2.7).

Soft warnings (5W1H partial / RAG semantic-dup / ambiguity) are never silently
swallowed: they become rule-generated questions (no LLM), and the human's
accountable disposition is recorded in the turn journal (``action=clarify``) plus
an auditable ``metadata.clarifications`` record. API and CLI offer the same
action over the shared store (Parity Rule).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tm.cli.intent_chat import register_session_commands
from tm.intent.clarify import (
    DISPOSITION_CONFIRMED_DISTINCT,
    DISPOSITION_MERGE,
    DISPOSITION_SUPPLEMENT,
    QKIND_AMBIGUITY,
    QKIND_PARTIAL_DIMENSION,
    QKIND_SEMANTIC_DUPLICATE,
    generate_questions,
)
from tm.intent.consistency_gate import SoftWarning
from tm.intent.session import (
    SessionTransitionError,
    advance,
    clarify,
    new_session,
    verify_clarifications,
    verify_journal,
)
from tm.intent.session_store import SessionStore, session_body_to_raw
from tm.server.routes_sessions import create_sessions_router

from tm.intent.session import GateFacts


# ─── question generation (deterministic, zero-LLM) ──────────────────


def test_generate_questions_from_soft_warnings():
    warns = (
        SoftWarning(kind="semantic_duplicate", message="near intent A", ref="intent.A"),
        SoftWarning(kind="ambiguity", message="'fair' undefined"),
    )
    qs = generate_questions(soft_warnings=warns)
    assert [q.id for q in qs] == ["q.0", "q.1"]
    assert qs[0].kind == QKIND_SEMANTIC_DUPLICATE and qs[0].refs == ("intent.A",)
    assert qs[1].kind == QKIND_AMBIGUITY
    assert DISPOSITION_MERGE in qs[0].options


def test_generate_questions_from_partial_dimensions():
    report = {
        "dimensions": {
            "who": {"status": "satisfied"},
            "why": {"status": "partial", "suggestion": "needs both context and goal"},
            "what": {"status": "partial", "missing_reason": "no inputs/outputs"},
        }
    }
    qs = generate_questions(completeness_report=report)
    kinds = {q.kind for q in qs}
    assert kinds == {QKIND_PARTIAL_DIMENSION}
    targets = {q.refs[0] for q in qs}
    assert targets == {"5w1h:why", "5w1h:what"}


def test_generate_questions_is_deterministic():
    warns = (SoftWarning(kind="ambiguity", message="m", ref="r"),)
    a = [q.to_dict() for q in generate_questions(soft_warnings=warns)]
    b = [q.to_dict() for q in generate_questions(soft_warnings=warns)]
    assert a == b


# ─── session.clarify records an accountable disposition ─────────────


def _session():
    s = new_session("session.clar", "intent.clar")
    return advance(s, GateFacts(), role="agent")  # at check_5w1h, one turn


def test_clarify_appends_turn_and_record():
    s = _session()
    before_step = s.current_step
    out = clarify(
        s,
        disposition=DISPOSITION_SUPPLEMENT,
        warning_ref="5w1h:why",
        reason="exposed a real gap",
        resolution="added success metric",
    )
    assert out.current_step == before_step  # clarify annotates, never advances
    last = out.turns[-1]
    assert last.action == "clarify" and last.role == "human"
    assert last.output_ref == "clar.0" and last.input_ref == "5w1h:why"
    record = out.metadata["clarifications"][0]
    assert record["disposition"] == DISPOSITION_SUPPLEMENT
    assert record["resolution"] == "added success metric"
    assert record["record_hash"]


def test_clarify_is_immutable():
    s = _session()
    n_turns = len(s.turns)
    clarify(s, disposition=DISPOSITION_MERGE, warning_ref="intent.A")
    assert len(s.turns) == n_turns
    assert "clarifications" not in (s.metadata or {})


def test_confirmed_distinct_requires_reason():
    s = _session()
    with pytest.raises(SessionTransitionError):
        clarify(s, disposition=DISPOSITION_CONFIRMED_DISTINCT, warning_ref="intent.A")
    ok = clarify(
        s,
        disposition=DISPOSITION_CONFIRMED_DISTINCT,
        warning_ref="intent.A",
        reason="different scope: reader vs author",
    )
    assert ok.metadata["clarifications"][0]["reason"].startswith("different scope")


def test_unknown_disposition_refused():
    s = _session()
    with pytest.raises(SessionTransitionError):
        clarify(s, disposition="ignore_it", warning_ref="x")


def test_clarify_refused_when_sealed():
    s = _session()
    sealed = replace(s, status="sealed")
    with pytest.raises(SessionTransitionError):
        clarify(sealed, disposition=DISPOSITION_MERGE)


# ─── audit: journal + clarification hashes intact ──────────────────


def test_clarify_keeps_journal_and_records_sound():
    s = _session()
    s = clarify(s, disposition=DISPOSITION_SUPPLEMENT, warning_ref="5w1h:why", reason="r")
    s = clarify(s, disposition=DISPOSITION_MERGE, warning_ref="intent.A", resolution="merged into A")
    assert verify_journal(s) == []
    assert verify_clarifications(s) == []


def test_tampered_clarification_record_is_detected():
    s = _session()
    s = clarify(s, disposition=DISPOSITION_MERGE, warning_ref="intent.A", resolution="merged")
    tampered_records = [dict(s.metadata["clarifications"][0])]
    tampered_records[0]["disposition"] = DISPOSITION_SUPPLEMENT  # stale hash
    tampered = replace(s, metadata={**s.metadata, "clarifications": tampered_records})
    assert verify_clarifications(tampered)


# ─── API + CLI parity ───────────────────────────────────────────────


def _app(store_dir: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(create_sessions_router(SessionStore(store_dir)))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_api_clarify_records_disposition(tmp_path: Path):
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json={"session_id": "session.demo", "root_intent_ref": "intent.demo"})
        resp = await client.post(
            "/api/v1/sessions/session.demo:clarify",
            json={"disposition": "supplement", "warning_ref": "5w1h:why", "resolution": "added metric"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["turns"][-1]["action"] == "clarify"
        assert body["metadata"]["clarifications"][0]["disposition"] == "supplement"


@pytest.mark.asyncio
async def test_api_clarify_confirmed_distinct_without_reason_409(tmp_path: Path):
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json={"session_id": "session.demo", "root_intent_ref": "intent.demo"})
        resp = await client.post(
            "/api/v1/sessions/session.demo:clarify",
            json={"disposition": "confirmed_distinct", "warning_ref": "intent.A"},
        )
        assert resp.status_code == 409


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    intent_parser = sub.add_parser("intent")
    intent_sub = intent_parser.add_subparsers(dest="intent_cmd")
    intent_sub.required = True
    register_session_commands(intent_sub)
    return parser


def _run_cli(argv: list[str]):
    args = _parser().parse_args(argv)
    return args.func(args)


def test_cli_clarify_matches_store(tmp_path: Path, capsys):
    store_dir = str(tmp_path / "s")
    _run_cli(["intent", "chat", "--new", "--intent", "intent.cli", "--session", "session.cli", "--sessions-dir", store_dir])
    capsys.readouterr()
    rc = _run_cli(
        [
            "intent", "session", "clarify", "session.cli",
            "--disposition", "merge", "--warning-ref", "intent.A",
            "--resolution", "merged", "--sessions-dir", store_dir,
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["turns"][-1]["action"] == "clarify"
    body = SessionStore(Path(store_dir)).load("session.cli")
    assert session_body_to_raw(body)["metadata"]["clarifications"][0]["disposition"] == "merge"
