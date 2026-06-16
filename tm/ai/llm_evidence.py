"""``llm_call`` evidence — Phase 7 Stage 7-1.1.

A **pure, deterministic, zero-LLM** builder for the ``llm_call`` evidence record
the plan reserves on ``ProofReport.evidence_chain``. It captures the auditable
fingerprint of a design-time LLM call — ``prompt_hash`` / ``response_hash`` /
``model`` / ``provider`` / token usage (and, when the call was a degraded
fallback, the trigger that caused it) — so every NL→formal candidate is
traceable and the same prompt is replayable.

This module imports **no provider and performs no network I/O**: it only hashes
strings and packs a dataclass. It is therefore safe to call from anywhere
(including the deterministic propose path) and is covered by the no-LLM-import
guard alongside the completeness modules.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from tm.artifacts.hash import body_hash

if TYPE_CHECKING:  # avoid importing provider/proof machinery at runtime
    from tm.ai.providers.base import LlmUsage
    from tm.control.meta.proof import EvidenceEntry

#: ``EvidenceEntry.event_type`` value for an LLM call (plan §LLM evidence 字段).
LLM_CALL_EVENT = "llm_call"


def text_hash(text: str) -> str:
    """Stable content hash of raw prompt / response text (sha256 hex)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LlmEvidence:
    """Auditable fingerprint of one design-time LLM call.

    Deterministic: identical (provider, model, prompt, response, usage) always
    yields the same :meth:`record_hash`. ``fallback_from`` is set only when this
    call was reached via degradation (see :mod:`tm.ai.fallback`), naming the
    trigger so the audit trail shows *why* a non-primary provider answered.
    """

    provider: str
    model: str
    prompt_hash: str
    response_hash: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Optional[float] = None
    fallback_from: Optional[str] = None

    @classmethod
    def from_call(
        cls,
        *,
        provider: str,
        model: str,
        prompt: str,
        response_text: str,
        usage: "LlmUsage | None" = None,
        fallback_from: Optional[str] = None,
    ) -> "LlmEvidence":
        pt = ct = tt = 0
        cost: Optional[float] = None
        if usage is not None:
            pt, ct, tt = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
            cost = usage.cost_usd
        return cls(
            provider=provider,
            model=model,
            prompt_hash=text_hash(prompt),
            response_hash=text_hash(response_text),
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            cost_usd=cost,
            fallback_from=fallback_from,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "cost_usd": self.cost_usd,
            "fallback_from": self.fallback_from,
        }

    def record_hash(self) -> str:
        """Canonical hash of the evidence record (commitment for the chain)."""
        return body_hash(self.to_dict())

    def to_evidence_entry(self, *, source: str = "ai.propose") -> "EvidenceEntry":
        """Pack into a :class:`EvidenceEntry` (``event_type="llm_call"``).

        The proof machinery is imported lazily so this module stays free of any
        runtime dependency on the control/proof stack.
        """
        from tm.control.meta.proof import EvidenceEntry

        return EvidenceEntry(source=source, event_type=LLM_CALL_EVENT, data=self.to_dict())


__all__ = ["LLM_CALL_EVENT", "LlmEvidence", "text_hash"]
