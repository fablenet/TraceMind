"""CLI surface for IntentSession — Phase 7 Stage 7-2.5 (Automation Plane, CLI side).

Mirrors the frozen ``SESSION_LIFECYCLE`` actions (``tm/intent/design_loop.py``)
one-for-one against :mod:`tm.server.routes_sessions`, backed by the same
:class:`tm.intent.session_store.SessionStore` (Parity Rule). Output convention
follows the rest of the intents CLI: a human-readable summary on **stderr**, the
canonical session JSON on **stdout**.

Commands (registered under ``tm intent``)::

    tm intent chat --new --intent <id> [--session <id>]   # new session
    tm intent chat --session <id>                          # resume (== show)
    tm intent session show <id>
    tm intent session resume <id>
    tm intent session list
    tm intent session advance <id> [--role ...] [--verify-passed] [--closed]
                                    [--completeness-no-error] [--note ...]
                                    [--sign-off-signer ...] [--sign-off-scope ...]
    tm intent session revert <id> --to <step> --reason <text> [--role ...]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from tm.intent.clarify import CLARIFY_DISPOSITIONS
from tm.intent.design_loop import DesignStep
from tm.intent.session import (
    GateFacts,
    SessionTransitionError,
    advance,
    clarify,
    completeness_no_error,
    new_session,
    revert,
    seal,
)
from tm.intent.uncertainty import load_dispositions
from tm.intent.session_store import SessionNotFound, SessionStore, session_body_to_raw

_DEFAULT_SESSIONS_DIR = os.getenv("TM_SESSIONS_DIR", ".tracemind/sessions")


def _store(args: argparse.Namespace) -> SessionStore:
    root = getattr(args, "sessions_dir", None) or _DEFAULT_SESSIONS_DIR
    return SessionStore(Path(root).expanduser())


def _emit(body: Any, *, summary: str) -> None:
    print(summary, file=sys.stderr)
    raw = session_body_to_raw(body)
    sys.stdout.write(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _summary(body: Any, action: str) -> str:
    return (
        f"session {action}: id={body.session_id} status={body.status} "
        f"step={body.current_step} turns={len(body.turns)}"
    )


# ─── chat (new / resume) ──────────────────────────────────────────


def _cmd_chat(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.new:
        if not args.intent:
            print("intent chat --new requires --intent <root_intent_ref>", file=sys.stderr)
            return 1
        session_id = args.session or f"session.{args.intent}.1"
        if store.exists(session_id):
            print(
                f"intent chat: session '{session_id}' already exists; "
                f"resume with `tm intent session resume {session_id}`",
                file=sys.stderr,
            )
            return 1
        try:
            body = new_session(session_id, args.intent)
            store.save(body)
        except Exception as exc:
            print(f"intent chat: {exc}", file=sys.stderr)
            return 1
        _emit(body, summary=_summary(body, "created"))
        return 0

    # resume == show current state
    if not args.session:
        print("intent chat: provide --new --intent <id> to start, or --session <id> to resume", file=sys.stderr)
        return 1
    try:
        body = store.load(args.session)
    except SessionNotFound:
        print(f"intent chat: session '{args.session}' not found", file=sys.stderr)
        return 1
    _emit(body, summary=_summary(body, "resumed"))
    return 0


# ─── session show / resume / list ─────────────────────────────────


def _cmd_session_show(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        body = store.load(args.session_id)
    except SessionNotFound:
        print(f"intent session show: '{args.session_id}' not found", file=sys.stderr)
        return 1
    _emit(body, summary=_summary(body, "show"))
    return 0


def _cmd_session_list(args: argparse.Namespace) -> int:
    store = _store(args)
    ids = store.list_ids()
    print(f"session list: {len(ids)} session(s) under {store.root}", file=sys.stderr)
    sys.stdout.write(json.dumps(ids, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


# ─── session advance / revert ─────────────────────────────────────


def _cmd_session_advance(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        body = store.load(args.session_id)
    except SessionNotFound:
        print(f"intent session advance: '{args.session_id}' not found", file=sys.stderr)
        return 1

    derived = completeness_no_error(body) if not args.completeness_no_error else True
    facts = GateFacts(
        completeness_no_error=derived,
        verify_passed=args.verify_passed,
        human_signoff_and_all_dims_closed=args.closed,
    )
    sign_off = None
    if args.sign_off_signer:
        from tm.artifacts.models import IntentSessionSignOff

        sign_off = IntentSessionSignOff(signer=args.sign_off_signer, scope=list(args.sign_off_scope or []))
    try:
        updated = advance(body, facts, role=args.role, sign_off=sign_off, note=args.note)
        store.save(updated)
    except SessionTransitionError as exc:
        req = f" (requirement={exc.requirement})" if exc.requirement else ""
        print(f"intent session advance: refused{req}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"intent session advance: {exc}", file=sys.stderr)
        return 1
    _emit(updated, summary=_summary(updated, "advanced"))
    return 0


def _cmd_session_revert(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        body = store.load(args.session_id)
    except SessionNotFound:
        print(f"intent session revert: '{args.session_id}' not found", file=sys.stderr)
        return 1
    try:
        to_step = DesignStep(args.to)
    except ValueError:
        print(f"intent session revert: unknown design step '{args.to}'", file=sys.stderr)
        return 1
    try:
        updated = revert(body, to_step, role=args.role, reason=args.reason)
        store.save(updated)
    except SessionTransitionError as exc:
        print(f"intent session revert: refused: {exc}", file=sys.stderr)
        return 1
    _emit(updated, summary=_summary(updated, "reverted"))
    return 0


def _cmd_session_clarify(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        body = store.load(args.session_id)
    except SessionNotFound:
        print(f"intent session clarify: '{args.session_id}' not found", file=sys.stderr)
        return 1
    try:
        updated = clarify(
            body,
            disposition=args.disposition,
            role=args.role,
            warning_ref=args.warning_ref,
            reason=args.reason or "",
            resolution=args.resolution or "",
        )
        store.save(updated)
    except SessionTransitionError as exc:
        print(f"intent session clarify: refused: {exc}", file=sys.stderr)
        return 1
    _emit(updated, summary=_summary(updated, "clarified"))
    return 0


def _cmd_session_seal(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        body = store.load(args.session_id)
    except SessionNotFound:
        print(f"intent session seal: '{args.session_id}' not found", file=sys.stderr)
        return 1
    dispositions = {}
    if args.dispositions:
        try:
            dispositions = load_dispositions(Path(args.dispositions))
        except Exception as exc:
            print(f"intent session seal: bad dispositions: {exc}", file=sys.stderr)
            return 1
    try:
        updated = seal(
            body,
            signer=args.signer,
            intent_path=Path(args.intent),
            profile=args.profile or "base",
            dispositions=dispositions,
            verify_passed=args.verify_passed,
            scope=list(args.scope) if args.scope else None,
            signed_at=args.signed_at,
        )
        store.save(updated)
    except SessionTransitionError as exc:
        req = f" (requirement={exc.requirement})" if exc.requirement else ""
        print(f"intent session seal: refused{req}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"intent session seal: {exc}", file=sys.stderr)
        return 1
    _emit(updated, summary=_summary(updated, "sealed"))
    return 0


# ─── registration (called from tm/cli/intent.py) ──────────────────


def _add_sessions_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-dir",
        dest="sessions_dir",
        help=f"session store directory (default: $TM_SESSIONS_DIR or {_DEFAULT_SESSIONS_DIR})",
    )


def register_session_commands(intent_sub: argparse._SubParsersAction) -> None:
    """Register ``chat`` + ``session ...`` under the existing ``tm intent`` group."""
    chat_parser = intent_sub.add_parser("chat", help="start or resume an iterative design session")
    chat_parser.add_argument("--new", action="store_true", help="create a new session")
    chat_parser.add_argument("--intent", help="root intent ref (required with --new)")
    chat_parser.add_argument("--session", help="session id (resume, or override new id)")
    _add_sessions_dir(chat_parser)
    chat_parser.set_defaults(func=_cmd_chat)

    session_parser = intent_sub.add_parser("session", help="inspect / drive an IntentSession")
    session_sub = session_parser.add_subparsers(dest="session_cmd")
    session_sub.required = True

    show = session_sub.add_parser("show", help="print a session's current state")
    show.add_argument("session_id")
    _add_sessions_dir(show)
    show.set_defaults(func=_cmd_session_show)

    resume = session_sub.add_parser("resume", help="resume a session (alias of show; state is in the artifact)")
    resume.add_argument("session_id")
    _add_sessions_dir(resume)
    resume.set_defaults(func=_cmd_session_show)

    listing = session_sub.add_parser("list", help="list stored session ids")
    _add_sessions_dir(listing)
    listing.set_defaults(func=_cmd_session_list)

    adv = session_sub.add_parser("advance", help="advance current_step iff the entry gate is satisfied")
    adv.add_argument("session_id")
    adv.add_argument("--role", choices=["human", "agent"], default="agent")
    adv.add_argument("--verify-passed", dest="verify_passed", action="store_true", help="verify gate fact (7-V)")
    adv.add_argument(
        "--closed",
        action="store_true",
        help="human_signoff_and_all_dims_closed gate fact (seal)",
    )
    adv.add_argument(
        "--completeness-no-error",
        dest="completeness_no_error",
        action="store_true",
        help="force the completeness gate fact (default: derived from the embedded 5W1H report)",
    )
    adv.add_argument("--note", help="optional note recorded on the transition turn")
    adv.add_argument("--sign-off-signer", dest="sign_off_signer", help="human signer id (required to seal)")
    adv.add_argument(
        "--sign-off-scope",
        dest="sign_off_scope",
        nargs="*",
        help="produced refs covered by the sign-off",
    )
    _add_sessions_dir(adv)
    adv.set_defaults(func=_cmd_session_advance)

    rev = session_sub.add_parser("revert", help="move current_step back to an earlier step (with a reason)")
    rev.add_argument("session_id")
    rev.add_argument("--to", required=True, help="target earlier design step")
    rev.add_argument("--reason", required=True, help="reason recorded in the journal")
    rev.add_argument("--role", choices=["human", "agent"], default="human")
    _add_sessions_dir(rev)
    rev.set_defaults(func=_cmd_session_revert)

    clr = session_sub.add_parser("clarify", help="record a soft-warning disposition in the journal (§4b)")
    clr.add_argument("session_id")
    clr.add_argument(
        "--disposition",
        required=True,
        choices=sorted(CLARIFY_DISPOSITIONS),
        help="confirmed_distinct (reason required) / merge / supplement / amend_constraint",
    )
    clr.add_argument("--role", choices=["human", "agent"], default="human")
    clr.add_argument("--warning-ref", dest="warning_ref", help="ref of the soft warning being dispositioned")
    clr.add_argument("--reason", help="rationale (required for confirmed_distinct)")
    clr.add_argument("--resolution", help="what was done (merge target / supplement / amendment)")
    _add_sessions_dir(clr)
    clr.set_defaults(func=_cmd_session_clarify)

    sl = session_sub.add_parser("seal", help="sign off & seal (closure invariant computed server-side, §4c)")
    sl.add_argument("session_id")
    sl.add_argument("--signer", required=True, help="human signer id (the requirement is signed off)")
    sl.add_argument("--intent", required=True, help="path to the produced intent (seal-mode 5W1H recompute)")
    sl.add_argument("--profile", help="5W1H profile (default: base)")
    sl.add_argument("--verify-passed", dest="verify_passed", action="store_true", help="CTL verify gate fact (7-V)")
    sl.add_argument("--dispositions", help="path to dimension->disposition closure (JSON/YAML)")
    sl.add_argument("--scope", nargs="*", help="produced refs covered by the sign-off")
    sl.add_argument("--signed-at", dest="signed_at", help="ISO timestamp recorded in the sign_off")
    _add_sessions_dir(sl)
    sl.set_defaults(func=_cmd_session_seal)


__all__ = ["register_session_commands"]
