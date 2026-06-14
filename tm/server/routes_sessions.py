"""IntentSession routes — Phase 7 Stage 7-2.5 (Automation Plane, HTTP side).

Exposes the frozen ``SESSION_LIFECYCLE`` actions (``tm/intent/design_loop.py``)
over HTTP, backed by the shared :class:`tm.intent.session_store.SessionStore` so
the API and CLI operate on the *same* on-disk session artifacts (Parity Rule):

- ``POST /api/v1/sessions``                — new session (bound to a root intent)
- ``GET  /api/v1/sessions/{id}``           — show / resume (state lives in artifact)
- ``POST /api/v1/sessions/{id}:advance``   — advance current_step iff entry gate holds
- ``POST /api/v1/sessions/{id}:revert``    — move current_step back with a logged reason
- ``POST /api/v1/sessions/{id}:clarify``   — record a soft-warning disposition in the journal

All transitions go through the deterministic, zero-LLM state machine in
:mod:`tm.intent.session`; the LLM is never on the critical path here.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from tm.artifacts.models import IntentSessionSignOff
from tm.artifacts.validator import ArtifactValidationError
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
from tm.intent.uncertainty import parse_dispositions
from tm.intent.session_store import SessionNotFound, SessionStore, session_body_to_raw


class NewSessionRequest(BaseModel):
    session_id: str
    root_intent_ref: str


class AdvanceRequest(BaseModel):
    role: str = "agent"
    verify_passed: bool = False
    closed: bool = Field(default=False, description="human_signoff_and_all_dims_closed gate fact")
    completeness_no_error: Optional[bool] = Field(
        default=None,
        description="override; when omitted the fact is derived from the session's embedded 5W1H report",
    )
    note: Optional[str] = None
    sign_off: Optional[Dict[str, Any]] = None


class RevertRequest(BaseModel):
    to_step: str
    reason: str
    role: str = "human"


class ClarifyRequest(BaseModel):
    disposition: str
    role: str = "human"
    warning_ref: Optional[str] = None
    question: Optional[Dict[str, Any]] = None
    reason: str = ""
    resolution: str = ""


class SealRequest(BaseModel):
    signer: str
    intent_path: str
    profile: str = "base"
    verify_passed: bool = False
    dispositions: Dict[str, Any] = Field(default_factory=dict)
    scope: Optional[list] = None
    candidate_hash: Optional[str] = None
    existing_hashes: list = Field(default_factory=list)
    signed_at: Optional[str] = None


def create_sessions_router(store: SessionStore) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sessions", tags=["sessions", "sessions.v1"])

    def _load(session_id: str):
        try:
            return store.load(session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"session '{session_id}' not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    def _save_and_return(body) -> Dict[str, Any]:
        try:
            store.save(body)
        except (ArtifactValidationError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return session_body_to_raw(body)

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_session(payload: NewSessionRequest) -> Dict[str, Any]:
        if store.exists(payload.session_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"session '{payload.session_id}' already exists",
            )
        try:
            body = new_session(payload.session_id, payload.root_intent_ref)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _save_and_return(body)

    @router.get("/{session_id}")
    def get_session(session_id: str) -> Dict[str, Any]:
        return session_body_to_raw(_load(session_id))

    @router.post("/{session_id}:advance")
    def advance_session(session_id: str, payload: AdvanceRequest) -> Dict[str, Any]:
        body = _load(session_id)
        derived = completeness_no_error(body) if payload.completeness_no_error is None else payload.completeness_no_error
        facts = GateFacts(
            completeness_no_error=derived,
            verify_passed=payload.verify_passed,
            human_signoff_and_all_dims_closed=payload.closed,
        )
        sign_off = None
        if payload.sign_off is not None:
            try:
                sign_off = IntentSessionSignOff.from_mapping(payload.sign_off)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        try:
            updated = advance(body, facts, role=payload.role, sign_off=sign_off, note=payload.note)
        except SessionTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": str(exc), "requirement": exc.requirement},
            ) from exc
        return _save_and_return(updated)

    @router.post("/{session_id}:revert")
    def revert_session(session_id: str, payload: RevertRequest) -> Dict[str, Any]:
        body = _load(session_id)
        try:
            to_step = DesignStep(payload.to_step)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown design step '{payload.to_step}'",
            ) from exc
        try:
            updated = revert(body, to_step, role=payload.role, reason=payload.reason)
        except SessionTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": str(exc), "requirement": exc.requirement},
            ) from exc
        return _save_and_return(updated)

    @router.post("/{session_id}:clarify")
    def clarify_session(session_id: str, payload: ClarifyRequest) -> Dict[str, Any]:
        body = _load(session_id)
        try:
            updated = clarify(
                body,
                disposition=payload.disposition,
                role=payload.role,
                question=payload.question,
                warning_ref=payload.warning_ref,
                reason=payload.reason,
                resolution=payload.resolution,
            )
        except SessionTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": str(exc), "requirement": exc.requirement},
            ) from exc
        return _save_and_return(updated)

    @router.post("/{session_id}:seal")
    def seal_session(session_id: str, payload: SealRequest) -> Dict[str, Any]:
        body = _load(session_id)
        from pathlib import Path

        try:
            dispositions = parse_dispositions(payload.dispositions)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        try:
            updated = seal(
                body,
                signer=payload.signer,
                intent_path=Path(payload.intent_path),
                profile=payload.profile,
                dispositions=dispositions,
                verify_passed=payload.verify_passed,
                scope=payload.scope,
                candidate_hash=payload.candidate_hash,
                existing_hashes=tuple(payload.existing_hashes),
                signed_at=payload.signed_at,
            )
        except SessionTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": str(exc), "requirement": exc.requirement},
            ) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _save_and_return(updated)

    return router


__all__ = [
    "AdvanceRequest",
    "ClarifyRequest",
    "NewSessionRequest",
    "RevertRequest",
    "SealRequest",
    "create_sessions_router",
]
