"""Workbench ↔ API/CLI Parity CI — Phase 7 Stage 7-5.2.

The Parity Rule (铁律): every interactive-plane (workbench) action MUST be
expressible by exactly one automation-plane API + one CLI; the workbench holds
**no UI-only capability** (invariant 2). This test makes that rule
machine-checkable by walking the *frozen* design-loop contract
(:mod:`tm.intent.design_loop`, Task 7-5.0) and asserting, against the real
server app and CLI parser, that:

1. **Contract integrity** — every row is well-formed; no duplicate / orphan /
   UI-only action; human-gated rows line up with ``HUMAN_ONLY_STEPS``; every
   gated and ordered ``DesignStep`` has a parity row.
2. **Live surface** — each *live* action's declared API route is registered AND
   its CLI command resolves; the live API surface matches an explicit roadmap
   pin, so landing a pending route (e.g. the 7-1 LLM propose/refine endpoints)
   forces wiring the CLI + parity together rather than drifting in silently.
3. **Cross-plane equivalence** — the orchestration plane (session lifecycle) is
   **byte-identical** across the API and the CLI when both drive the same
   on-disk session store.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tm.cli import _build_parser
from tm.cli.intent_chat import register_session_commands
from tm.intent.design_loop import (
    DESIGN_STEP_ORDER,
    ENTRY_GATES,
    HUMAN_ONLY_STEPS,
    PARITY_MATRIX,
    SESSION_LIFECYCLE,
    DesignStep,
    ParityEntry,
)
from tm.intent.session_store import SessionStore
from tm.server.app import create_app
from tm.server.config import ServerConfig

ALL_ROWS: tuple[ParityEntry, ...] = (*PARITY_MATRIX, *SESSION_LIFECYCLE)

#: Roadmap pin — actions whose dedicated v1 API route is registered *today*.
#: Everything else in PARITY_MATRIX (draft intent / check 5w1h / propose /
#: refine / instantiate / verify / accept) is contract-declared but its v1
#: endpoint lands with its owning stage (7-1 etc.). Updating this set is a
#: deliberate, reviewed act that must accompany the new route + CLI + parity.
EXPECTED_LIVE_ACTIONS: frozenset[str] = frozenset(
    {"seal", "new session", "show / current step", "advance", "revert", "clarify", "resume"}
)


# ─── introspection helpers ──────────────────────────────────────────


def _norm_path(path: str) -> str:
    """Collapse path params so ``{id}`` and ``{session_id}`` compare equal."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _api_tuple(api: str) -> tuple[str, str]:
    method, _, path = api.partition(" ")
    return method, _norm_path(path)


def _registered_routes(app: FastAPI) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            routes.add((method, _norm_path(route.path)))
    return routes


def _cli_alternatives(cli: str) -> list[tuple[str, ...]]:
    """Leading command tokens for each ``/``-separated CLI alternative."""
    alts: list[tuple[str, ...]] = []
    for alt in cli.split(" / "):
        alt = alt.strip()
        if alt.startswith("tm "):
            alt = alt[3:]
        toks: list[str] = []
        for tok in alt.split():
            if tok.startswith("-") or tok.startswith("{"):  # flags / positional placeholders
                break
            toks.append(tok)
        if toks:
            alts.append(tuple(toks))
    return alts


def _cli_command_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse introspection is the supported way
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                here = (*prefix, name)
                paths.add(here)
                paths |= _cli_command_paths(sub, here)
    return paths


# ─── 1. contract integrity (pure) ───────────────────────────────────


def test_contract_rows_wellformed() -> None:
    for e in ALL_ROWS:
        assert e.action and e.api and e.cli and e.equivalence, f"empty field in {e.action!r}"
        method, _, path = e.api.partition(" ")
        assert method in {"POST", "GET"}, f"{e.action}: bad API verb {method!r}"
        assert path.startswith("/api/"), f"{e.action}: API path not /api/* ({path!r})"
        assert e.cli.startswith("tm "), f"{e.action}: CLI must start with 'tm ' ({e.cli!r})"
        assert _cli_alternatives(e.cli), f"{e.action}: no CLI command tokens"


def test_action_names_unique() -> None:
    actions = [e.action for e in ALL_ROWS]
    assert len(actions) == len(set(actions)), "duplicate action name in parity contract"


def test_human_gate_aligns_with_human_only_steps() -> None:
    for e in PARITY_MATRIX:
        if e.step is None:
            continue
        assert e.human_gate == (e.step in HUMAN_ONLY_STEPS), (
            f"{e.action}: human_gate={e.human_gate} but step {e.step.value} "
            f"{'is' if e.step in HUMAN_ONLY_STEPS else 'is not'} human-only"
        )


def test_every_gated_and_ordered_step_has_parity_row() -> None:
    steps = {e.step for e in PARITY_MATRIX if e.step is not None}
    for gate in ENTRY_GATES:
        assert gate.step in steps, f"gated step {gate.step.value} has no parity row"
    for step in DESIGN_STEP_ORDER:
        assert step in steps, f"design step {step.value} has no parity row"


# ─── 2. live surface against the real app + CLI ──────────────────────


def test_live_api_surface_matches_roadmap_pin() -> None:
    registered = _registered_routes(create_app(ServerConfig(base_dir=Path("/tmp/tm-parity-app"))))
    live = {e.action for e in ALL_ROWS if _api_tuple(e.api) in registered}
    assert live == set(EXPECTED_LIVE_ACTIONS), (
        "live API surface drifted from the roadmap pin; if you implemented a new "
        f"contract route, update EXPECTED_LIVE_ACTIONS (added={live - set(EXPECTED_LIVE_ACTIONS)}, "
        f"removed={set(EXPECTED_LIVE_ACTIONS) - live})"
    )


def test_live_actions_have_fully_resolvable_cli() -> None:
    cmd_paths = _cli_command_paths(_build_parser())
    for e in ALL_ROWS:
        if e.action not in EXPECTED_LIVE_ACTIONS:
            continue
        resolved = any(alt in cmd_paths for alt in _cli_alternatives(e.cli))
        assert resolved, f"live action {e.action!r} CLI {e.cli!r} does not resolve to a command"


def test_no_cli_orphan_action() -> None:
    cmd_paths = _cli_command_paths(_build_parser())
    top_level = {p[0] for p in cmd_paths}
    for e in ALL_ROWS:
        first_token = _cli_alternatives(e.cli)[0][0]
        assert first_token in top_level, f"{e.action!r}: CLI group {first_token!r} not a tm command"


# ─── 3. cross-plane byte-identical equivalence (orchestration) ───────


def _app(store_dir: Path) -> FastAPI:
    app = FastAPI()
    from tm.server.routes_sessions import create_sessions_router

    app.include_router(create_sessions_router(SessionStore(store_dir)))
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    intent_parser = sub.add_parser("intent")
    intent_sub = intent_parser.add_subparsers(dest="intent_cmd")
    intent_sub.required = True
    register_session_commands(intent_sub)
    return parser


def _run_cli(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


@pytest.mark.asyncio
async def test_session_lifecycle_byte_identical_across_planes(tmp_path: Path, capsys) -> None:
    """The same new→advance→advance→revert sequence yields a byte-identical
    canonical session JSON whether driven via the HTTP API or the CLI."""
    sid, iid = "session.parity", "intent.parity"

    # API plane
    api_dir = tmp_path / "api"
    app = _app(api_dir)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/api/v1/sessions", json={"session_id": sid, "root_intent_ref": iid})
        await client.post(f"/api/v1/sessions/{sid}:advance", json={"role": "agent"})
        await client.post(f"/api/v1/sessions/{sid}:advance", json={"role": "agent"})
        await client.post(
            f"/api/v1/sessions/{sid}:revert",
            json={"to_step": "draft", "reason": "redo", "role": "human"},
        )
        api_body = (await client.get(f"/api/v1/sessions/{sid}")).json()

    # CLI plane (same session id, fresh store)
    cli_dir = str(tmp_path / "cli")
    _run_cli(["intent", "chat", "--new", "--intent", iid, "--session", sid, "--sessions-dir", cli_dir])
    _run_cli(["intent", "session", "advance", sid, "--role", "agent", "--sessions-dir", cli_dir])
    _run_cli(["intent", "session", "advance", sid, "--role", "agent", "--sessions-dir", cli_dir])
    capsys.readouterr()
    _run_cli(
        ["intent", "session", "revert", sid, "--to", "draft", "--reason", "redo", "--role", "human", "--sessions-dir", cli_dir]
    )
    cli_body = json.loads(capsys.readouterr().out)

    assert cli_body == api_body
    assert cli_body["current_step"] == "draft"
    # the journal hash chain is identical too (turn_hash is session-id independent)
    assert [t["turn_hash"] for t in cli_body["turns"]] == [t["turn_hash"] for t in api_body["turns"]]
