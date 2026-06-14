"""IntentSession persistence — Phase 7 Stage 7-2.5.

The session *state lives entirely in the artifact* (workbench-api §3 / Stage 7-2
goal): there is no server- or frontend-side session memory. This module is the
single, deterministic file-per-session backend shared by **both** planes — the
HTTP API (:mod:`tm.server.routes_sessions`) and the CLI
(:mod:`tm.cli.intent_chat`) — so the Parity Rule holds by construction.

Each session is stored as one ``<session_id>.json`` K-artifact (envelope + v0.4
``IntentSession`` body). Saves are full-document overwrites (a session is a
mutable *working* document until sealed); reads reconstruct the
:class:`IntentSessionBody` via its schema-checked ``from_mapping``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

from tm.artifacts.hash import body_hash
from tm.artifacts.models import (
    IntentSessionBody,
    IntentSessionSignOff,
    IntentSessionTurn,
)
from tm.artifacts.types import ArtifactType
from tm.artifacts.validator import validate_intent_session_spec

_SESSION_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class SessionNotFound(KeyError):
    """Raised when a session id has no stored artifact."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── serialization (shared by API + CLI for output) ────────────────


def _turn_to_raw(turn: IntentSessionTurn) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"seq": turn.seq, "role": turn.role, "action": turn.action}
    for key in ("input_ref", "output_ref", "provider", "turn_hash"):
        value = getattr(turn, key)
        if value is not None:
            raw[key] = value
    return raw


def _sign_off_to_raw(sign_off: IntentSessionSignOff) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"signer": sign_off.signer}
    if sign_off.scope:
        raw["scope"] = list(sign_off.scope)
    if sign_off.completeness_snapshot:
        raw["completeness_snapshot"] = dict(sign_off.completeness_snapshot)
    if sign_off.dispositions:
        raw["dispositions"] = dict(sign_off.dispositions)
    for key in ("gate_report_hash", "signed_at", "sign_hash"):
        value = getattr(sign_off, key)
        if value is not None:
            raw[key] = value
    return raw


def session_body_to_raw(body: IntentSessionBody) -> Dict[str, Any]:
    """Serialize an :class:`IntentSessionBody` to its canonical body dict.

    Inverse of ``IntentSessionBody.from_mapping``; the result satisfies the
    ``IntentSessionSpec`` schema. Optional empty fields (completeness / sign_off
    / metadata) are omitted to keep the document minimal and the body hash
    stable.
    """
    raw: Dict[str, Any] = {
        "session_id": body.session_id,
        "root_intent_ref": body.root_intent_ref,
        "status": body.status,
        "current_step": body.current_step,
        "turns": [_turn_to_raw(t) for t in body.turns],
        "produced_refs": list(body.produced_refs),
    }
    if body.completeness is not None:
        raw["completeness"] = body.completeness
    if body.sign_off is not None:
        raw["sign_off"] = _sign_off_to_raw(body.sign_off)
    if body.metadata:
        raw["metadata"] = dict(body.metadata)
    return raw


# ─── store ─────────────────────────────────────────────────────────


class SessionStore:
    """File-per-session store rooted at ``root`` (one JSON artifact each)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"invalid session_id '{session_id}'; must match {_SESSION_ID_RE.pattern}"
            )
        return session_id

    def _path(self, session_id: str) -> Path:
        return self.root / f"{self._validate_id(session_id)}.json"

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def list_ids(self) -> List[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def load(self, session_id: str) -> IntentSessionBody:
        path = self._path(session_id)
        if not path.exists():
            raise SessionNotFound(session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError(f"session artifact {path} is not a mapping")
        body_raw = data.get("body")
        if not isinstance(body_raw, Mapping):
            raise ValueError(f"session artifact {path} missing 'body' section")
        return IntentSessionBody.from_mapping(body_raw)

    def save(self, body: IntentSessionBody, *, created_by: str = "tm.intent.session") -> Path:
        raw = session_body_to_raw(body)
        validate_intent_session_spec(raw)  # raises ArtifactValidationError on schema breach
        envelope = {
            "artifact_id": self._validate_id(body.session_id),
            "status": "accepted" if body.status == "sealed" else "candidate",
            "artifact_type": ArtifactType.INTENT_SESSION.value,
            "version": "v0.4",
            "created_by": created_by,
            "created_at": _now_iso(),
            "body_hash": body_hash(raw),
            "envelope_hash": "",
            "meta": {},
        }
        document = {"envelope": envelope, "body": raw}
        path = self._path(body.session_id)
        path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        return path


__all__ = [
    "SessionNotFound",
    "SessionStore",
    "session_body_to_raw",
]
