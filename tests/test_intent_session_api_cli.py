"""IntentSession API + CLI surface and parity (Stage 7-2.5).

Both planes drive the same deterministic state machine over the same on-disk
:class:`SessionStore`, so the Parity Rule holds: a session created via the API
is readable via the CLI and vice versa, byte-for-byte.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tm.cli.intent_chat import register_session_commands
from tm.intent.session_store import SessionStore, session_body_to_raw
from tm.server.routes_sessions import create_sessions_router


# ─── API harness ────────────────────────────────────────────────────


def _app(store_dir: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(create_sessions_router(SessionStore(store_dir)))
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


_NEW = {"session_id": "session.demo", "root_intent_ref": "intent.demo"}


@pytest.mark.asyncio
async def test_create_show_roundtrip(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        created = await client.post("/api/v1/sessions", json=_NEW)
        assert created.status_code == 201
        body = created.json()
        assert body["session_id"] == "session.demo"
        assert body["status"] == "working"
        assert body["current_step"] == "draft"

        shown = await client.get("/api/v1/sessions/session.demo")
        assert shown.status_code == 200
        assert shown.json() == body


@pytest.mark.asyncio
async def test_create_duplicate_conflicts(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json=_NEW)
        dup = await client.post("/api/v1/sessions", json=_NEW)
        assert dup.status_code == 409


@pytest.mark.asyncio
async def test_get_missing_is_404(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        resp = await client.get("/api/v1/sessions/session.nope")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_advance_ungated_then_gate_blocks(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json=_NEW)
        # draft → check_5w1h → propose → refine (all ungated)
        for expected in ("check_5w1h", "propose", "refine"):
            r = await client.post("/api/v1/sessions/session.demo:advance", json={"role": "agent"})
            assert r.status_code == 200, r.text
            assert r.json()["current_step"] == expected
        # refine → verify needs completeness_no_error; none embedded → 409
        blocked = await client.post("/api/v1/sessions/session.demo:advance", json={"role": "agent"})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["requirement"] == "completeness_no_error"


@pytest.mark.asyncio
async def test_advance_with_forced_gate_then_human_only(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json=_NEW)
        for _ in range(3):
            await client.post("/api/v1/sessions/session.demo:advance", json={"role": "agent"})
        # force completeness gate → reach verify
        r = await client.post(
            "/api/v1/sessions/session.demo:advance",
            json={"role": "agent", "completeness_no_error": True},
        )
        assert r.json()["current_step"] == "verify"
        # verify → accept is human-only; agent is refused
        denied = await client.post(
            "/api/v1/sessions/session.demo:advance",
            json={"role": "agent", "verify_passed": True},
        )
        assert denied.status_code == 409


@pytest.mark.asyncio
async def test_revert_logs_reason(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json=_NEW)
        for _ in range(3):  # to refine
            await client.post("/api/v1/sessions/session.demo:advance", json={"role": "agent"})
        rev = await client.post(
            "/api/v1/sessions/session.demo:revert",
            json={"to_step": "check_5w1h", "reason": "goal changed", "role": "human"},
        )
        assert rev.status_code == 200
        body = rev.json()
        assert body["current_step"] == "check_5w1h"
        assert "goal changed" in body["turns"][-1]["output_ref"]


@pytest.mark.asyncio
async def test_revert_unknown_step_is_400(tmp_path: Path) -> None:
    app = _app(tmp_path / "s")
    async with _client(app) as client:
        await client.post("/api/v1/sessions", json=_NEW)
        rev = await client.post(
            "/api/v1/sessions/session.demo:revert",
            json={"to_step": "nirvana", "reason": "x"},
        )
        assert rev.status_code == 400


# ─── CLI harness ────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    intent_parser = sub.add_parser("intent")
    intent_sub = intent_parser.add_subparsers(dest="intent_cmd")
    intent_sub.required = True
    register_session_commands(intent_sub)
    return parser


def _run_cli(argv: list[str]) -> tuple[int, argparse.Namespace]:
    parser = _parser()
    args = parser.parse_args(argv)
    return args.func(args), args


def test_cli_chat_new_and_show(tmp_path: Path, capsys) -> None:
    store_dir = str(tmp_path / "s")
    rc, _ = _run_cli(
        ["intent", "chat", "--new", "--intent", "intent.demo", "--session", "session.cli", "--sessions-dir", store_dir]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "session.cli"
    assert out["current_step"] == "draft"

    rc, _ = _run_cli(["intent", "session", "show", "session.cli", "--sessions-dir", store_dir])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "session.cli"


def test_cli_advance_and_revert(tmp_path: Path, capsys) -> None:
    store_dir = str(tmp_path / "s")
    _run_cli(["intent", "chat", "--new", "--intent", "intent.demo", "--session", "session.cli", "--sessions-dir", store_dir])
    capsys.readouterr()
    rc, _ = _run_cli(["intent", "session", "advance", "session.cli", "--role", "agent", "--sessions-dir", store_dir])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["current_step"] == "check_5w1h"

    rc, _ = _run_cli(
        ["intent", "session", "revert", "session.cli", "--to", "draft", "--reason", "redo", "--sessions-dir", store_dir]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["current_step"] == "draft"


def test_cli_advance_gate_refused_returns_1(tmp_path: Path, capsys) -> None:
    store_dir = str(tmp_path / "s")
    _run_cli(["intent", "chat", "--new", "--intent", "intent.demo", "--session", "session.cli", "--sessions-dir", store_dir])
    for _ in range(3):  # to refine
        _run_cli(["intent", "session", "advance", "session.cli", "--role", "agent", "--sessions-dir", store_dir])
    capsys.readouterr()
    rc, _ = _run_cli(["intent", "session", "advance", "session.cli", "--role", "agent", "--sessions-dir", store_dir])
    assert rc == 1
    assert "completeness_no_error" in capsys.readouterr().err


# ─── cross-plane parity ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_created_session_is_readable_by_cli(tmp_path: Path, capsys) -> None:
    store_dir = tmp_path / "s"
    app = _app(store_dir)
    async with _client(app) as client:
        created = await client.post("/api/v1/sessions", json={"session_id": "session.x", "root_intent_ref": "intent.x"})
        api_body = created.json()

    rc, _ = _run_cli(["intent", "session", "show", "session.x", "--sessions-dir", str(store_dir)])
    assert rc == 0
    cli_body = json.loads(capsys.readouterr().out)
    assert cli_body == api_body


def test_cli_created_session_matches_store_serialization(tmp_path: Path, capsys) -> None:
    store_dir = tmp_path / "s"
    _run_cli(["intent", "chat", "--new", "--intent", "intent.y", "--session", "session.y", "--sessions-dir", str(store_dir)])
    capsys.readouterr()
    body = SessionStore(store_dir).load("session.y")
    assert session_body_to_raw(body)["root_intent_ref"] == "intent.y"
