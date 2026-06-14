"""Ambiguity-clarification: soft warnings → questions → accountable disposition.

Stage 7-2.7 (plan §4b). Clarification is not noise — it is a **missing-discovery
mechanism**: when the consistency gate (7-0.8) raises a *soft* warning (a 5W1H
dimension is ``partial``, RAG flags two intents/constraints as semantically near,
or a slot value is ambiguous), surfacing it as a concrete question makes the
designer realise whether they have a design gap.

This module is deterministic and zero-LLM (guarded): the question candidates are
**rule-generated** so the loop can clarify with no LLM at all (invariant 2 — an
LLM may later *polish* the wording, never produce the question). The human then
dispositions each warning into one of four accountable answers, which is
recorded in the turn journal (see :func:`tm.intent.session.clarify`):

* ``confirmed_distinct`` — not a duplicate/conflict; **a reason is required**;
* ``merge``             — deduplicate the two near-identical items;
* ``supplement``        — add the requirement the warning exposed as missing;
* ``amend_constraint``  — change a constraint to resolve a suspected conflict.

A soft warning is therefore never silently swallowed: it is either consciously
confirmed harmless (with a recorded reason) or turned into a real requirement
supplement / correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from tm.intent.completeness import ALL_DIMENSIONS
from tm.intent.consistency_gate import SoftWarning

DISPOSITION_CONFIRMED_DISTINCT = "confirmed_distinct"
DISPOSITION_MERGE = "merge"
DISPOSITION_SUPPLEMENT = "supplement"
DISPOSITION_AMEND_CONSTRAINT = "amend_constraint"

CLARIFY_DISPOSITIONS: frozenset[str] = frozenset(
    {
        DISPOSITION_CONFIRMED_DISTINCT,
        DISPOSITION_MERGE,
        DISPOSITION_SUPPLEMENT,
        DISPOSITION_AMEND_CONSTRAINT,
    }
)

#: Dispositions that assert "no change needed" and so MUST carry a reason for
#: accountability (the designer takes responsibility for confirming harmless).
_REASON_REQUIRED: frozenset[str] = frozenset({DISPOSITION_CONFIRMED_DISTINCT})

QKIND_SEMANTIC_DUPLICATE = "semantic_duplicate"
QKIND_AMBIGUITY = "ambiguity"
QKIND_PARTIAL_DIMENSION = "partial_dimension"
QKIND_GENERIC = "generic"


@dataclass(frozen=True)
class ClarificationQuestion:
    """A rule-generated clarification question candidate."""

    id: str
    kind: str
    prompt: str
    refs: tuple[str, ...] = ()
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "refs": list(self.refs),
            "options": list(self.options),
        }


def disposition_requires_reason(disposition: str) -> bool:
    return disposition in _REASON_REQUIRED


def _semantic_duplicate_q(qid: str, w: SoftWarning) -> ClarificationQuestion:
    ref = w.ref or "the related item"
    return ClarificationQuestion(
        id=qid,
        kind=QKIND_SEMANTIC_DUPLICATE,
        prompt=(
            f"This looks semantically close to {ref}: {w.message}. "
            f"Are they genuinely distinct, or should they be merged? "
            f"（与 {ref} 语义相近：是确属不同，还是应合并去重？）"
        ),
        refs=(w.ref,) if w.ref else (),
        options=(DISPOSITION_CONFIRMED_DISTINCT, DISPOSITION_MERGE, DISPOSITION_AMEND_CONSTRAINT),
    )


def _ambiguity_q(qid: str, w: SoftWarning) -> ClarificationQuestion:
    return ClarificationQuestion(
        id=qid,
        kind=QKIND_AMBIGUITY,
        prompt=(
            f"This is ambiguous: {w.message}. Please disambiguate, or supplement the "
            f"missing detail it exposes.（存在二义：请澄清，或补充其暴露的缺失需求。）"
        ),
        refs=(w.ref,) if w.ref else (),
        options=(DISPOSITION_SUPPLEMENT, DISPOSITION_CONFIRMED_DISTINCT, DISPOSITION_AMEND_CONSTRAINT),
    )


def _generic_q(qid: str, w: SoftWarning) -> ClarificationQuestion:
    return ClarificationQuestion(
        id=qid,
        kind=QKIND_GENERIC,
        prompt=f"Please review this advisory: {w.message}.（请复核该软告警。）",
        refs=(w.ref,) if w.ref else (),
        options=tuple(sorted(CLARIFY_DISPOSITIONS)),
    )


def _partial_dimension_q(qid: str, dim_value: str, entry: Mapping[str, Any]) -> ClarificationQuestion:
    hint = entry.get("suggestion") or entry.get("missing_reason") or "incomplete"
    return ClarificationQuestion(
        id=qid,
        kind=QKIND_PARTIAL_DIMENSION,
        prompt=(
            f"Dimension '{dim_value}' is only partially specified ({hint}). "
            f"Supplement it, or confirm the partial is intentional.（维度 '{dim_value}' "
            f"仅部分完整：请补充，或确认有意为之。）"
        ),
        refs=(f"5w1h:{dim_value}",),
        options=(DISPOSITION_SUPPLEMENT, DISPOSITION_CONFIRMED_DISTINCT),
    )


def generate_questions(
    *,
    soft_warnings: Iterable[SoftWarning] = (),
    completeness_report: Mapping[str, Any] | None = None,
) -> list[ClarificationQuestion]:
    """Deterministically derive clarification questions from gate signals.

    Sources (plan §4b): each :class:`SoftWarning` (RAG semantic-duplicate /
    ambiguity / generic) and every ``partial`` 5W1H dimension in the embedded
    completeness report. Order is stable: soft warnings in their given order,
    then partial dimensions in ``ALL_DIMENSIONS`` order. Ids are ``q.0, q.1, …``.
    """
    questions: list[ClarificationQuestion] = []
    for w in soft_warnings:
        qid = f"q.{len(questions)}"
        if w.kind == QKIND_SEMANTIC_DUPLICATE:
            questions.append(_semantic_duplicate_q(qid, w))
        elif w.kind == QKIND_AMBIGUITY:
            questions.append(_ambiguity_q(qid, w))
        else:
            questions.append(_generic_q(qid, w))

    if completeness_report is not None:
        dims = completeness_report.get("dimensions")
        if isinstance(dims, Mapping):
            for dim in ALL_DIMENSIONS:
                entry = dims.get(dim.value)
                if isinstance(entry, Mapping) and entry.get("status") == "partial":
                    qid = f"q.{len(questions)}"
                    questions.append(_partial_dimension_q(qid, dim.value, entry))
    return questions


__all__ = [
    "CLARIFY_DISPOSITIONS",
    "DISPOSITION_AMEND_CONSTRAINT",
    "DISPOSITION_CONFIRMED_DISTINCT",
    "DISPOSITION_MERGE",
    "DISPOSITION_SUPPLEMENT",
    "QKIND_AMBIGUITY",
    "QKIND_GENERIC",
    "QKIND_PARTIAL_DIMENSION",
    "QKIND_SEMANTIC_DUPLICATE",
    "ClarificationQuestion",
    "disposition_requires_reason",
    "generate_questions",
]
