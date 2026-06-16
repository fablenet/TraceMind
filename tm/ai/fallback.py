"""Provider fallback chain + triggers — Phase 7 Stage 7-1.3.

Invariant 2 in force: the LLM front-door is **degradable**. When a primary
provider (external API / local 27B) is unreachable, times out, or is rate
limited, :class:`FallbackProvider` transparently degrades to a secondary
provider (ultimately :class:`~tm.ai.providers.fake.FakeProvider`, which is
always online), so the NL→formal pipeline never stalls on LLM availability.

The four DoD fallback triggers (plan §失败 fallback 触发条件) are named in
:class:`FallbackTrigger`. This module owns the three *provider-level* triggers
(``unreachable`` / ``timeout`` / ``rate_limit``); the ``schema_invalid`` trigger
fires one layer up (the propose step, when a syntactically fine response fails
the PatternProposal schema), and ``user_switch`` is an explicit caller choice.
All five are exercised in CI with zero tokens (injected ``LlmError`` / fake).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from tm.ai.providers.base import ErrorCode, LlmCallResult, LlmError, Provider


class FallbackTrigger(str, Enum):
    """Why a non-primary provider answered (the four DoD triggers + rate limit)."""

    UNREACHABLE = "unreachable"      # transport/provider error
    TIMEOUT = "timeout"              # request exceeded timeout
    RATE_LIMIT = "rate_limit"        # provider throttled
    SCHEMA_INVALID = "schema_invalid"  # response failed downstream schema (propose layer)
    USER_SWITCH = "user_switch"      # caller explicitly selected the fallback


#: Map a provider :data:`ErrorCode` to the fallback trigger it should record.
_CODE_TO_TRIGGER: dict[ErrorCode, FallbackTrigger] = {
    "PROVIDER_ERROR": FallbackTrigger.UNREACHABLE,
    "RUN_TIMEOUT": FallbackTrigger.TIMEOUT,
    "QUEUE_TIMEOUT": FallbackTrigger.TIMEOUT,
    "RATE_LIMIT": FallbackTrigger.RATE_LIMIT,
}

#: Error codes that should degrade to the fallback rather than propagate.
#: ``BAD_REQUEST`` / ``RUN_CANCELLED`` are *not* degradable — a malformed
#: request or an explicit cancel is a real error, not an availability blip.
DEGRADABLE_CODES: frozenset[ErrorCode] = frozenset(_CODE_TO_TRIGGER)


def trigger_for_code(code: ErrorCode) -> FallbackTrigger:
    return _CODE_TO_TRIGGER.get(code, FallbackTrigger.UNREACHABLE)


@dataclass
class FallbackEvent:
    """Records that degradation happened (for evidence ``fallback_from``)."""

    trigger: FallbackTrigger
    from_provider: str
    to_provider: str
    detail: str = ""


class FallbackProvider(Provider):
    """Try ``primary``; on a degradable :class:`LlmError`, fall back to ``fallback``.

    The most recent degradation is recorded on :attr:`last_event` so the caller
    can stamp ``fallback_from`` into the ``llm_call`` evidence. Non-degradable
    errors (``BAD_REQUEST`` / ``RUN_CANCELLED``) propagate unchanged.
    """

    def __init__(
        self,
        primary: Provider,
        fallback: Provider,
        *,
        primary_name: str = "primary",
        fallback_name: str = "fallback",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self.last_event: Optional[FallbackEvent] = None

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        temperature: float | None = None,
        top_p: float | None = None,
        timeout_s: float | None = None,
    ) -> LlmCallResult:
        try:
            result = await self._primary.complete(
                model=model, prompt=prompt, temperature=temperature, top_p=top_p, timeout_s=timeout_s
            )
            self.last_event = None
            return result
        except LlmError as exc:
            if exc.code not in DEGRADABLE_CODES:
                raise
            self.last_event = FallbackEvent(
                trigger=trigger_for_code(exc.code),
                from_provider=self._primary_name,
                to_provider=self._fallback_name,
                detail=exc.message,
            )
            return await self._fallback.complete(
                model=model, prompt=prompt, temperature=temperature, top_p=top_p, timeout_s=timeout_s
            )


__all__ = [
    "DEGRADABLE_CODES",
    "FallbackEvent",
    "FallbackProvider",
    "FallbackTrigger",
    "trigger_for_code",
]
