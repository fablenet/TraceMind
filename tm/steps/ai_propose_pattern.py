"""Step ``ai.propose_pattern_instances`` — Stage 5-4 task 4.1.

**Natural language → candidate :class:`PropertyPatternBody` instances**
backed by RAG over the Pattern Library + Case Corpus (tasks 4.2 / 4.3).

## Two paths, same contract

Phase 5 invariant 4 (every LLM step has an equivalent non-LLM path)
applies here in full force. This step ships two implementations behind
the same I/O schema:

| ``provider`` value | Behaviour |
|---|---|
| ``fake`` / ``none`` | Pure RAG: pattern keyword retrieval picks candidates, slot hints from the caller fill the slots. **Zero LLM calls, deterministic, runnable in CI.** |
| ``openai`` (and other real providers) | RAG seeds a constrained-output prompt; the LLM must produce JSON conforming to :class:`PatternProposal`. **Validation is identical to the non-LLM path** (same dataclass, same governance verifier). |

In Phase 5 only the fake/none path is wired up — adding a real provider
later requires no API change downstream because both paths emit the
same :class:`PatternProposal` shape.

## Output schema

A successful run returns ``{status: "ok", candidates: [...], retrieval: ...}``.
Each candidate is a JSON-serializable dict of :class:`PatternProposal`,
ready to feed into ``tm.patterns.instantiate.instantiate_pattern`` or
into ``compile_intent_to_bundle`` (task 4.5).

## Why no LLM yet?

Stage 5-4 ships the **plumbing** (RAG layer + proposal contract);
plugging a real LLM in is a downstream concern that depends on the
provider's available models, prompt budget, and the user's allow-list
discipline. The non-LLM path is the **always-on baseline** — if a real
LLM ever fails or is unavailable, this step still produces useful
candidates.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tm.kb import (
    CaseCorpus,
    CaseStructuredRetriever,
    PatternKeywordRetriever,
    RetrievalHit,
)
from tm.patterns import PatternLibrary

STEP_NAME = "ai.propose_pattern_instances"


# ─── Output dataclass ─────────────────────────────────────────────


@dataclass
class PatternProposal:
    """A single candidate pattern instance proposed by the step.

    Carries enough context to:

    - feed straight into :func:`tm.patterns.instantiate.instantiate_pattern`
    - explain itself to a human reviewer via :attr:`rationale`
    - be audited (via :attr:`source` and :attr:`score`)
    """

    pattern_id: str
    slot_fills: Dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    rationale: str = ""
    source: str = "rag_pattern"
    missing_slots: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "slot_fills": dict(self.slot_fills),
            "score": self.score,
            "rationale": self.rationale,
            "source": self.source,
            "missing_slots": list(self.missing_slots),
        }


# ─── Synchronous core (testable, no asyncio) ──────────────────────


def propose_pattern_instances(
    nl_text: str,
    library: PatternLibrary,
    *,
    corpus: CaseCorpus | None = None,
    slot_hints: Mapping[str, Mapping[str, str]] | None = None,
    limit: int = 5,
    category: str | None = None,
    provider: str = "fake",
) -> List[PatternProposal]:
    """Produce ranked :class:`PatternProposal` candidates.

    Pure-function core that powers the async :func:`run` step and the
    CLI (task 4.1 already wires this into ``tm pattern instantiate``'s
    AI mode in a future iteration).

    Args:
        nl_text: Natural-language description of the goal /
            constraint. Used for retrieval ranking only — never
            embedded blindly into the output.
        library: Pattern library to search.
        corpus: Optional :class:`CaseCorpus`. When provided, the case
            retriever's hits are added to ``rationale`` for any pattern
            that has prior cases. Useful for warm-start RAG.
        slot_hints: ``{pattern_id: {slot_name: value}}`` map. When the
            non-LLM path proposes a pattern, it copies matching hints
            into the proposal's ``slot_fills``; unsupplied slots are
            listed in :attr:`PatternProposal.missing_slots`.
        limit: Maximum number of candidates to return.
        category: Optional filter (``safety`` / ``liveness`` / ``fairness``).
        provider: ``"fake"`` / ``"none"`` for the pure-RAG non-LLM path,
            other values are reserved for future real-LLM integration
            and currently raise :class:`NotImplementedError`.

    Returns:
        Ranked list of :class:`PatternProposal` objects.

    Raises:
        NotImplementedError: When ``provider`` is not ``fake``/``none``.
    """
    provider_norm = (provider or "").lower().strip()
    if provider_norm not in {"fake", "none", ""}:
        raise NotImplementedError(
            f"Provider '{provider}' integration is reserved for future "
            "stages — Stage 5-4 only ships the RAG / non-LLM path. "
            "Pattern proposal logic is identical regardless of provider, "
            "so wiring a real LLM is purely a prompt + JSON-schema task."
        )

    pattern_retriever = PatternKeywordRetriever(library)
    pattern_hits = pattern_retriever.query(nl_text, limit=limit, category=category)

    case_retriever = CaseStructuredRetriever(corpus) if corpus is not None else None

    hints = dict(slot_hints or {})
    proposals: List[PatternProposal] = []
    for hit in pattern_hits:
        entry = library.get(hit.ref)
        provided_slots = dict(hints.get(hit.ref, {}))
        all_slots = [s.name for s in entry.body.slots]
        missing = [s for s in all_slots if s not in provided_slots]

        rationale_parts = [_rationale_from_pattern_hit(hit, nl_text)]
        case_evidence_count = 0
        if case_retriever is not None:
            case_hits = case_retriever.query(nl_text, pattern_id=hit.ref, limit=3)
            case_evidence_count = len(case_hits)
            if case_hits:
                rationale_parts.append(_rationale_from_case_hits(case_hits))

        # Score: pattern keyword score + small case-evidence boost.
        # Cases provide light grounding — they don't dominate the ranking
        # since their existence is orthogonal to NL semantic match.
        score = hit.score + 0.05 * min(case_evidence_count, 3)

        proposals.append(
            PatternProposal(
                pattern_id=hit.ref,
                slot_fills=provided_slots,
                score=round(score, 6),
                rationale="; ".join(p for p in rationale_parts if p),
                source="rag_pattern",
                missing_slots=missing,
            )
        )

    proposals.sort(key=lambda p: (-p.score, p.pattern_id))
    return proposals[:limit]


def _rationale_from_pattern_hit(hit: RetrievalHit, nl_text: str) -> str:
    template = hit.payload.get("formula_template", "")
    category = hit.payload.get("category", "")
    head = f"Matched pattern '{hit.ref}' (category={category}, score={hit.score:.3f})"
    if template:
        return f"{head}; template={template}"
    return head


def _rationale_from_case_hits(case_hits: Sequence[RetrievalHit]) -> str:
    refs = ", ".join(h.ref for h in case_hits[:3])
    return f"prior cases: {refs}"


# ─── Step parameter validation ────────────────────────────────────


@dataclass
class _ProposeRequest:
    nl_text: str
    provider: str
    library_root: Optional[str]
    limit: int
    category: Optional[str]
    slot_hints: Dict[str, Dict[str, str]]


def _parse_request(params: Mapping[str, Any]) -> _ProposeRequest:
    nl_text = str(params.get("nl_text", "")).strip()
    if not nl_text:
        raise ValueError("nl_text is required and must be a non-empty string")
    provider = str(params.get("provider", "fake")).strip().lower()
    library_root = params.get("library_root")
    if library_root is not None:
        library_root = str(library_root)
    limit_raw = params.get("limit", 5)
    if not isinstance(limit_raw, int) or limit_raw <= 0:
        raise ValueError("limit must be a positive integer")
    category = params.get("category")
    if category is not None:
        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must be a non-empty string if provided")
        category = category.strip().lower()
    hints_raw = params.get("slot_hints") or {}
    if not isinstance(hints_raw, Mapping):
        raise ValueError("slot_hints must be an object")
    parsed_hints: Dict[str, Dict[str, str]] = {}
    for pattern_id, slot_map in hints_raw.items():
        if not isinstance(pattern_id, str):
            raise ValueError("slot_hints keys must be pattern_id strings")
        if not isinstance(slot_map, Mapping):
            raise ValueError(f"slot_hints[{pattern_id}] must be an object of slot_name → value strings")
        cast: Dict[str, str] = {}
        for slot_name, value in slot_map.items():
            if not isinstance(slot_name, str) or not isinstance(value, str):
                raise ValueError(f"slot_hints[{pattern_id}] entries must be str→str")
            cast[slot_name] = value
        parsed_hints[pattern_id] = cast
    return _ProposeRequest(
        nl_text=nl_text,
        provider=provider,
        library_root=library_root,
        limit=limit_raw,
        category=category,
        slot_hints=parsed_hints,
    )


# ─── Async step entry point ───────────────────────────────────────


async def run(
    params: dict[str, Any],
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
) -> dict:
    """Async step entry — wires :func:`propose_pattern_instances` into
    the runtime ``ai.*`` step protocol.

    Loads the pattern library (and case corpus, if a registry path is
    given) according to ``params`` and runs the synchronous core.
    Async-only because the real-LLM future variant will need to await
    a provider call; the current path returns immediately.
    """
    overall_start = time.perf_counter()
    try:
        request = _parse_request(params)
    except ValueError as exc:
        return {
            "status": "error",
            "error_code": "BAD_REQUEST",
            "reason": str(exc),
        }

    # Load library (defaults to seed library)
    try:
        from pathlib import Path

        from tm.patterns import load_seed_patterns, PatternLibrary

        if request.library_root:
            library = PatternLibrary.from_directory(Path(request.library_root))
        else:
            library = load_seed_patterns()
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        return {
            "status": "error",
            "error_code": "LIBRARY_LOAD_FAILED",
            "reason": str(exc),
        }

    # Load case corpus only if a registry path is provided
    corpus: CaseCorpus | None = None
    registry_path = params.get("registry_path")
    if registry_path:
        try:
            from pathlib import Path

            from tm.artifacts.registry import ArtifactRegistry
            from tm.artifacts.storage import RegistryStorage
            from tm.kb import build_case_corpus

            corpus = build_case_corpus(ArtifactRegistry(storage=RegistryStorage(Path(str(registry_path)))))
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            return {
                "status": "error",
                "error_code": "CORPUS_LOAD_FAILED",
                "reason": str(exc),
            }

    try:
        proposals = propose_pattern_instances(
            request.nl_text,
            library,
            corpus=corpus,
            slot_hints=request.slot_hints,
            limit=request.limit,
            category=request.category,
            provider=request.provider,
        )
    except NotImplementedError as exc:
        return {
            "status": "error",
            "error_code": "PROVIDER_NOT_SUPPORTED",
            "reason": str(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "status": "error",
            "error_code": "PROPOSE_FAILED",
            "reason": str(exc),
        }

    duration_ms = (time.perf_counter() - overall_start) * 1000.0
    return {
        "status": "ok",
        "provider": request.provider,
        "candidates": [p.to_dict() for p in proposals],
        "candidates_json": json.dumps(
            [p.to_dict() for p in proposals],
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        "duration_ms": duration_ms,
    }


# ─── Optional step-registry hook (parallel with ai.plan) ──────────


try:  # pragma: no cover - optional auto-registration
    from tm.steps.registry import register_step

    register_step(STEP_NAME, run)
except Exception:
    pass


__all__ = [
    "PatternProposal",
    "STEP_NAME",
    "propose_pattern_instances",
    "run",
]
