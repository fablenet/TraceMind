"""Step ``ai.refine_pattern`` — Phase 7 Stage 7-2.4.

**Multi-turn refinement of candidate :class:`PatternProposal` instances.**
Where :mod:`tm.steps.ai_propose_pattern` produces the *initial* ranked
candidate set (turn 0), this step advances the design loop one refinement turn
at a time: the human reacts to the current candidates (fill a slot, reject the
top one, pick one) and the step deterministically folds that reaction into a
new candidate set.

## Two paths, same contract (invariant 2 + Phase 7 pause condition)

| ``provider`` value | Behaviour |
|---|---|
| ``fake`` / ``none`` | **Pure rule-based refine**: slot values fill slots; reject drops the top candidate and promotes the next ranked one; missing dimensions surface as deterministic ``suggestions``. **Zero LLM calls, deterministic, runnable in CI.** |
| ``openai`` (and other real providers) | An LLM *may* improve candidate quality, but it is **never required to make progress**. Since no real provider is wired in this stage, a non-fake provider transparently **degrades to the deterministic path** (``degraded=True``) — exactly the "LLM failure ⇒ fall back to fake" contract. |

The hard Phase 7 pause-condition invariant lives here: *every* "advance to the
next turn" is drivable by the fake path alone. The LLM only polishes candidate
quality; it is never on the critical path of the design loop. The state machine
(:mod:`tm.intent.session`) gates advancement on deterministic facts, never on a
provider call.

## Output schema

``refine_candidates`` returns a :class:`RefineResult`; the async :func:`run`
step serialises it to ``{status, provider, candidates, candidates_json,
suggestions, degraded, applied}``. Each candidate is a
:class:`tm.steps.ai_propose_pattern.PatternProposal` dict, ready to feed back
into the next refine turn or into ``instantiate_pattern`` once accepted.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tm.patterns import PatternLibrary
from tm.steps.ai_propose_pattern import PatternProposal

STEP_NAME = "ai.refine_pattern"

_DETERMINISTIC_PROVIDERS = {"fake", "none", ""}

#: The deterministic refine vocabulary. ``note`` is a no-op carrier used by the
#: workbench to journal a human remark without changing candidates.
REFINE_KINDS = frozenset({"fill_slot", "reject", "select", "note"})


# ─── Refine action (deterministic instruction) ────────────────────


@dataclass(frozen=True)
class RefineAction:
    """One human reaction folded into the candidate set.

    ``target_pattern_id`` defaults to the current top candidate when omitted.
    ``slot_values`` is only consumed by ``kind == 'fill_slot'``.
    """

    kind: str
    target_pattern_id: Optional[str] = None
    slot_values: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RefineAction":
        if not isinstance(data, Mapping):
            raise ValueError("refine action must be an object")
        kind = str(data.get("kind", "")).strip().lower()
        if kind not in REFINE_KINDS:
            raise ValueError(f"refine action kind must be one of {sorted(REFINE_KINDS)}, got '{kind}'")
        target = data.get("target_pattern_id")
        if target is not None and not isinstance(target, str):
            raise ValueError("target_pattern_id must be a string if provided")
        slot_values_raw = data.get("slot_values") or {}
        if not isinstance(slot_values_raw, Mapping):
            raise ValueError("slot_values must be an object of slot_name → value strings")
        slot_values: Dict[str, str] = {}
        for name, value in slot_values_raw.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("slot_values entries must be str → str")
            slot_values[name] = value
        return cls(kind=kind, target_pattern_id=target, slot_values=slot_values)


@dataclass
class RefineResult:
    candidates: List[PatternProposal]
    suggestions: List[str] = field(default_factory=list)
    applied: str = "noop"
    degraded: bool = False
    provider: str = "fake"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "suggestions": list(self.suggestions),
            "applied": self.applied,
            "degraded": self.degraded,
            "provider": self.provider,
        }


# ─── Deterministic core ───────────────────────────────────────────


def _select_index(candidates: Sequence[PatternProposal], target_pattern_id: Optional[str]) -> int:
    if not candidates:
        raise ValueError("no candidates to refine")
    if target_pattern_id is None:
        return 0
    for idx, cand in enumerate(candidates):
        if cand.pattern_id == target_pattern_id:
            return idx
    raise ValueError(f"target pattern '{target_pattern_id}' is not among the current candidates")


def _missing_slots(library: PatternLibrary, pattern_id: str, fills: Mapping[str, str]) -> List[str]:
    entry = library.get(pattern_id)
    return [slot.name for slot in entry.body.slots if slot.name not in fills]


def refine_candidates(
    candidates: Sequence[PatternProposal],
    action: RefineAction,
    library: PatternLibrary,
    *,
    provider: str = "fake",
) -> RefineResult:
    """Fold one :class:`RefineAction` into the candidate set, deterministically.

    Never re-ranks on slot fills (preserves the original propose ordering); only
    ``reject`` (drop top / named) and ``select`` (promote named to front) change
    membership / order. ``fill_slot`` validates that every slot name is declared
    by the target pattern (the schema check is identical regardless of provider)
    and recomputes ``missing_slots``.

    A non-deterministic provider transparently degrades to this exact path
    (``degraded=True``) — the LLM is never required to advance.
    """
    provider_norm = (provider or "").strip().lower()
    degraded = provider_norm not in _DETERMINISTIC_PROVIDERS

    work: List[PatternProposal] = [replace(c, slot_fills=dict(c.slot_fills)) for c in candidates]
    applied = "noop"

    if action.kind == "fill_slot":
        idx = _select_index(work, action.target_pattern_id)
        target = work[idx]
        valid = {slot.name for slot in library.get(target.pattern_id).body.slots}
        unknown = [name for name in action.slot_values if name not in valid]
        if unknown:
            raise ValueError(
                f"slot(s) {sorted(unknown)} not declared by pattern '{target.pattern_id}'; "
                f"declared: {sorted(valid)}"
            )
        new_fills = {**target.slot_fills, **action.slot_values}
        missing = _missing_slots(library, target.pattern_id, new_fills)
        work[idx] = replace(target, slot_fills=new_fills, missing_slots=missing)
        applied = f"fill_slot:{target.pattern_id}:{','.join(sorted(action.slot_values))}"

    elif action.kind == "reject":
        idx = _select_index(work, action.target_pattern_id)
        removed = work.pop(idx)
        applied = f"reject:{removed.pattern_id}"

    elif action.kind == "select":
        idx = _select_index(work, action.target_pattern_id)
        chosen = work.pop(idx)
        work.insert(0, chosen)
        applied = f"select:{chosen.pattern_id}"

    elif action.kind == "note":
        applied = "note"

    else:  # pragma: no cover - guarded by RefineAction.from_mapping
        raise ValueError(f"unknown refine kind '{action.kind}'")

    suggestions = list(work[0].missing_slots) if work else []
    return RefineResult(
        candidates=work,
        suggestions=suggestions,
        applied=applied,
        degraded=degraded,
        provider=provider_norm or "fake",
    )


# ─── Step parameter parsing ───────────────────────────────────────


def _proposal_from_dict(data: Mapping[str, Any]) -> PatternProposal:
    if not isinstance(data, Mapping):
        raise ValueError("each candidate must be an object")
    pattern_id = data.get("pattern_id")
    if not isinstance(pattern_id, str) or not pattern_id:
        raise ValueError("candidate.pattern_id is required")
    slot_fills_raw = data.get("slot_fills") or {}
    if not isinstance(slot_fills_raw, Mapping):
        raise ValueError("candidate.slot_fills must be an object")
    slot_fills = {str(k): str(v) for k, v in slot_fills_raw.items()}
    missing_raw = data.get("missing_slots") or []
    if not isinstance(missing_raw, Sequence) or isinstance(missing_raw, str):
        raise ValueError("candidate.missing_slots must be a list")
    return PatternProposal(
        pattern_id=pattern_id,
        slot_fills=slot_fills,
        score=float(data.get("score", 0.0)),
        rationale=str(data.get("rationale", "")),
        source=str(data.get("source", "rag_pattern")),
        missing_slots=[str(s) for s in missing_raw],
    )


@dataclass
class _RefineRequest:
    candidates: List[PatternProposal]
    action: RefineAction
    provider: str
    library_root: Optional[str]


def _parse_request(params: Mapping[str, Any]) -> _RefineRequest:
    candidates_raw = params.get("candidates")
    if not isinstance(candidates_raw, Sequence) or isinstance(candidates_raw, str):
        raise ValueError("candidates is required and must be a list of proposal objects")
    candidates = [_proposal_from_dict(c) for c in candidates_raw]
    action_raw = params.get("action")
    if action_raw is None:
        raise ValueError("action is required")
    action = RefineAction.from_mapping(action_raw)
    provider = str(params.get("provider", "fake")).strip().lower()
    library_root = params.get("library_root")
    if library_root is not None:
        library_root = str(library_root)
    return _RefineRequest(
        candidates=candidates,
        action=action,
        provider=provider,
        library_root=library_root,
    )


# ─── Async step entry point ───────────────────────────────────────


async def run(
    params: dict[str, Any],
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
) -> dict:
    """Async step entry — wires :func:`refine_candidates` into the ``ai.*``
    step protocol. Loads the pattern library, parses one refine turn, applies it
    deterministically, and returns the new candidate set + suggestions."""
    overall_start = time.perf_counter()
    try:
        request = _parse_request(params)
    except ValueError as exc:
        return {"status": "error", "error_code": "BAD_REQUEST", "reason": str(exc)}

    try:
        from tm.patterns import load_seed_patterns

        if request.library_root:
            library = PatternLibrary.from_directory(Path(request.library_root))
        else:
            library = load_seed_patterns()
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        return {"status": "error", "error_code": "LIBRARY_LOAD_FAILED", "reason": str(exc)}

    try:
        result = refine_candidates(
            request.candidates,
            request.action,
            library,
            provider=request.provider,
        )
    except ValueError as exc:
        return {"status": "error", "error_code": "REFINE_FAILED", "reason": str(exc)}
    except KeyError as exc:  # unknown pattern in candidate vs library
        return {"status": "error", "error_code": "UNKNOWN_PATTERN", "reason": str(exc)}

    duration_ms = (time.perf_counter() - overall_start) * 1000.0
    payload = result.to_dict()
    return {
        "status": "ok",
        "provider": result.provider,
        "degraded": result.degraded,
        "applied": result.applied,
        "candidates": payload["candidates"],
        "suggestions": payload["suggestions"],
        "candidates_json": json.dumps(payload["candidates"], separators=(",", ":"), ensure_ascii=False),
        "duration_ms": duration_ms,
    }


try:  # pragma: no cover - optional auto-registration
    from tm.steps.registry import register_step

    register_step(STEP_NAME, run)
except Exception:
    pass


__all__ = [
    "REFINE_KINDS",
    "RefineAction",
    "RefineResult",
    "STEP_NAME",
    "refine_candidates",
    "run",
]
