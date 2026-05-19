"""Retrieval layer — Stage 5-4 task 4.3.

Pluggable retrieval over the Pattern Library and Case Corpus. Two
**file-level** backends ship now; vector index is a **reserved seam** for
when corpus size justifies the dependency.

## Why pluggable?

The retrieval layer is the most likely place to evolve (ripgrep →
jsonschema query → vector → hybrid). The :class:`Retriever` Protocol
freezes the *consumer* contract (``query`` returns scored
:class:`RetrievalHit` objects) so ``ai.propose_pattern_instances`` and
downstream RAG can swap backends without code changes.

## What ships in this stage

| Backend | Scope | When to use |
|---|---|---|
| :class:`PatternKeywordRetriever` | Pattern library | NL → matching patterns by keyword (CLI / fake provider path) |
| :class:`CaseStructuredRetriever` | Case corpus | Filter cases by pattern_id, intent_id, has-failure, evidence kind |
| :class:`VectorRetriever` (stub) | Reserved | Returns :class:`NotImplementedError`; documented protocol so a future contribution can plug in without API churn |

## Determinism

All backends sort hits by (score desc, ref asc) for reproducible CI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

from tm.patterns import PatternLibrary

from .case_corpus import Case, CaseCorpus


@dataclass(frozen=True)
class RetrievalHit:
    """A single ranked result from a :class:`Retriever`.

    Carries enough context (``kind``, ``ref``, ``score``, ``snippet``,
    ``payload``) for an LLM prompt or a CLI list view, while keeping the
    backing object behind ``payload`` so callers can drill in.
    """

    kind: str
    ref: str
    score: float
    snippet: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    """Common contract for all retrievers.

    Implementations should be **side-effect free**, deterministic for
    fixed inputs, and tolerant of empty queries.
    """

    def query(self, text: str, *, limit: int = 5, **filters: Any) -> List[RetrievalHit]: ...


# ─── Pattern keyword retriever ────────────────────────────────────


class PatternKeywordRetriever:
    """Token-overlap scoring over the Pattern Library.

    Tokenizes both the query string and each pattern's
    ``title + description + formula_template`` once at construction
    time, then ranks patterns by Jaccard overlap. Designed to be fast,
    explainable, and to give the *fake* LLM provider a sensible non-LLM
    candidate set.

    For Stage 5-4 this is good enough — the corpus is < 100 patterns. A
    future :class:`VectorRetriever` swap is trivial.
    """

    _TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")

    def __init__(self, library: PatternLibrary) -> None:
        self._library = library
        self._index: dict[str, set[str]] = {}
        for entry in library.entries():
            text = " ".join(
                [
                    entry.body.title or "",
                    entry.body.description or "",
                    entry.body.formula_template or "",
                    entry.category or "",
                    " ".join(slot.name for slot in entry.body.slots),
                ]
            )
            self._index[entry.pattern_id] = set(tok.lower() for tok in self._TOKEN_RE.findall(text))

    def query(
        self,
        text: str,
        *,
        limit: int = 5,
        category: str | None = None,
        **_extra: Any,
    ) -> List[RetrievalHit]:
        tokens = set(tok.lower() for tok in self._TOKEN_RE.findall(text))
        hits: List[RetrievalHit] = []
        for pid, doc_tokens in self._index.items():
            entry = self._library.get(pid)
            if category and entry.category != category:
                continue
            if not tokens:
                # Empty query → still return everything but with score 0
                score = 0.0
            else:
                overlap = tokens & doc_tokens
                if not overlap and not _matches_pattern_id(tokens, pid):
                    continue
                score = _jaccard_score(tokens, doc_tokens, pid)
            hits.append(
                RetrievalHit(
                    kind="pattern",
                    ref=pid,
                    score=score,
                    snippet=entry.body.title,
                    payload={
                        "category": entry.category,
                        "formula_template": entry.body.formula_template,
                        "slots": [s.name for s in entry.body.slots],
                    },
                )
            )
        hits.sort(key=lambda h: (-h.score, h.ref))
        return hits[:limit]


def _jaccard_score(query_tokens: set[str], doc_tokens: set[str], pattern_id: str) -> float:
    overlap = query_tokens & doc_tokens
    if not overlap:
        # Allow weak hit if the query explicitly contains a pattern_id segment
        if _matches_pattern_id(query_tokens, pattern_id):
            return 0.1
        return 0.0
    union = query_tokens | doc_tokens
    base = len(overlap) / len(union)
    # Boost when query mentions the pattern_id directly
    if _matches_pattern_id(query_tokens, pattern_id):
        base += 0.25
    return min(base, 1.0)


def _matches_pattern_id(query_tokens: set[str], pattern_id: str) -> bool:
    segments = re.split(r"[._-]", pattern_id.lower())
    return any(seg in query_tokens for seg in segments if seg)


# ─── Case structured retriever ────────────────────────────────────


class CaseStructuredRetriever:
    """Filter / search over a :class:`CaseCorpus` by structural predicates.

    The ``text`` parameter is searched naively (substring on intent_id
    and on evidence summaries); the real selection power is in the
    keyword filters (``pattern_id``, ``has_failures``, ``evidence_kind``).
    Built for RAG retrieval where the LLM (or fake provider) already
    knows which pattern it's working with.
    """

    def __init__(self, corpus: CaseCorpus) -> None:
        self._corpus = corpus

    def query(
        self,
        text: str,
        *,
        limit: int = 5,
        pattern_id: str | None = None,
        has_failures: bool | None = None,
        evidence_kind: str | None = None,
        **_extra: Any,
    ) -> List[RetrievalHit]:
        text_lower = text.lower()
        candidates: Iterable[Case]
        if pattern_id is not None:
            candidates = self._corpus.cases_for_pattern(pattern_id)
        else:
            candidates = self._corpus.cases()

        hits: List[RetrievalHit] = []
        for case in candidates:
            if has_failures is not None and case.has_failures() is not has_failures:
                continue
            if evidence_kind is not None and not case.evidence_of_kind(evidence_kind):
                continue
            score = _score_case_against_text(case, text_lower)
            if text_lower and score == 0.0:
                continue
            hits.append(
                RetrievalHit(
                    kind="case",
                    ref=case.intent_id,
                    score=score,
                    snippet=_case_snippet(case),
                    payload={
                        "pattern_refs": list(case.pattern_refs),
                        "evidence_kinds": sorted({ev.kind for ev in case.evidence}),
                        "has_failures": case.has_failures(),
                    },
                )
            )
        hits.sort(key=lambda h: (-h.score, h.ref))
        return hits[:limit]


def _score_case_against_text(case: Case, text_lower: str) -> float:
    """Substring match with light boost for failure cases.

    Failure cases are usually the most informative for RAG, so we
    surface them first when the query is otherwise tied.
    """
    if not text_lower:
        return 1.0 if case.has_failures() else 0.5
    score = 0.0
    if text_lower in case.intent_id.lower():
        score += 0.5
    for ev in case.evidence:
        if text_lower in ev.summary.lower():
            score += 0.25
            break
    if score > 0.0 and case.has_failures():
        score += 0.1
    return min(score, 1.0)


def _case_snippet(case: Case) -> str:
    parts = [case.intent_id]
    if case.pattern_refs:
        parts.append(f"patterns={','.join(case.pattern_refs[:3])}")
    if case.evidence:
        kinds = sorted({ev.kind for ev in case.evidence})
        parts.append(f"evidence={','.join(kinds)}")
    return " ".join(parts)


# ─── Ripgrep helper (optional, opportunistic) ─────────────────────


def ripgrep_available() -> bool:
    """True if a ``rg`` binary is on PATH.

    Used by :func:`ripgrep_search` and surfaced so callers can degrade
    gracefully (e.g. fall back to :class:`PatternKeywordRetriever`).
    """
    return shutil.which("rg") is not None


def ripgrep_search(
    text: str,
    *,
    root: Path,
    max_results: int = 50,
) -> List[RetrievalHit]:
    """Run ``rg --json`` over ``root`` and translate hits.

    Returns an empty list if ``rg`` is not available. Each hit carries
    the matching file path as ``ref`` and the snippet line. Not used
    inside the production RAG path (which prefers the structured
    retrievers above) but available as an emergency / debug tool.
    """
    if not ripgrep_available():
        return []
    try:
        proc = subprocess.run(
            ["rg", "--json", "--no-heading", "--smart-case", text, str(root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []

    hits: List[RetrievalHit] = []
    import json

    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path_obj = (data.get("path") or {}).get("text", "")
        lines = (data.get("lines") or {}).get("text", "").rstrip("\n")
        line_no = data.get("line_number") or 0
        hits.append(
            RetrievalHit(
                kind="ripgrep",
                ref=path_obj,
                score=1.0,
                snippet=lines[:200],
                payload={"line_number": line_no},
            )
        )
        if len(hits) >= max_results:
            break
    return hits


# ─── Vector backend (reserved seam) ───────────────────────────────


class VectorRetriever:
    """Reserved seam for future vector-similarity retrieval.

    A concrete implementation should:

    - Accept the same :class:`PatternLibrary` + :class:`CaseCorpus`
      constructor inputs as the keyword retrievers (so callers can
      swap backends transparently)
    - Honour the :class:`Retriever` Protocol exactly
    - Build embeddings lazily on first ``query`` call and cache them
      on disk (vector index is an optional dependency the L1/L2 user
      shouldn't pay for unless they ask)

    This stub raises :class:`NotImplementedError` so anyone wiring it up
    pre-implementation gets a clear failure. The class deliberately
    accepts arbitrary kwargs to absorb future dependencies (embedder,
    index_path, …) without breaking earlier call-sites.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def query(self, text: str, *, limit: int = 5, **filters: Any) -> List[RetrievalHit]:
        raise NotImplementedError(
            "VectorRetriever is a reserved seam — Phase 5 ships only "
            "keyword + structured retrievers. Switch to "
            "PatternKeywordRetriever or CaseStructuredRetriever, or "
            "contribute an embedder per the docstring."
        )


# ─── Bundle helper for RAG callers ────────────────────────────────


@dataclass
class RetrievalBundle:
    """Convenience aggregation of all readily available retrievers.

    ``ai.propose_pattern_instances`` (task 4.1) uses this to fan a query
    out across pattern + case backends in one call, returning a fused
    ranked list with retrieval source tags intact.
    """

    pattern_retriever: PatternKeywordRetriever
    case_retriever: CaseStructuredRetriever
    vector_retriever: Optional[Retriever] = None

    def query(
        self,
        text: str,
        *,
        limit: int = 5,
        category: str | None = None,
        pattern_id: str | None = None,
        **_extra: Any,
    ) -> List[RetrievalHit]:
        hits: List[RetrievalHit] = []
        hits.extend(self.pattern_retriever.query(text, limit=limit, category=category))
        hits.extend(self.case_retriever.query(text, limit=limit, pattern_id=pattern_id))
        if self.vector_retriever is not None:
            try:
                hits.extend(self.vector_retriever.query(text, limit=limit))
            except NotImplementedError:
                pass
        hits.sort(key=lambda h: (-h.score, h.kind, h.ref))
        return hits[:limit]


def make_default_bundle(library: PatternLibrary, corpus: CaseCorpus) -> RetrievalBundle:
    """Build a default :class:`RetrievalBundle` with the two ready
    backends and no vector retriever wired in."""
    return RetrievalBundle(
        pattern_retriever=PatternKeywordRetriever(library),
        case_retriever=CaseStructuredRetriever(corpus),
        vector_retriever=None,
    )


__all__ = [
    "CaseStructuredRetriever",
    "PatternKeywordRetriever",
    "Retriever",
    "RetrievalBundle",
    "RetrievalHit",
    "VectorRetriever",
    "make_default_bundle",
    "ripgrep_available",
    "ripgrep_search",
]


# Suppress unused import warnings for re-exported types
_ = (Sequence,)
