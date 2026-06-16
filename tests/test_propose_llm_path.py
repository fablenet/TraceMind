"""LLM-backed propose path + RAG fallback — Stage 7-1.4 (zero-token).

Drives :func:`propose_with_llm` and the ``ai.propose_pattern_instances`` step
with injected offline clients / stub providers — no network, no tokens. Covers
the success path (``source="llm"`` + ``llm_call`` evidence) and all four DoD
fallback triggers degrading to the deterministic RAG baseline.
"""

from __future__ import annotations

import json

import pytest

from tm.ai.llm_client import AsyncLLMClient
from tm.ai.providers.base import LlmCallResult, LlmError, LlmUsage, Provider
from tm.patterns import load_seed_patterns
from tm.steps.ai_propose_pattern import propose_with_llm, run

_NL = "eventually the queue is drained"
_PID = "liveness.eventually_x_holds"
_VALID = json.dumps(
    {"candidates": [{"pattern_id": _PID, "slot_fills": {"goal_predicate": "queue_drained"}, "rationale": "matches"}]}
)


class TextProvider(Provider):
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, *, model, prompt, temperature=None, top_p=None, timeout_s=None):
        return LlmCallResult(output_text=self.text, usage=LlmUsage(5, 5, 10, 0.0), raw=None)


class ErrorProvider(Provider):
    def __init__(self, code) -> None:
        self.code = code

    async def complete(self, *, model, prompt, temperature=None, top_p=None, timeout_s=None):
        raise LlmError(self.code, f"injected {self.code}")


def _client(provider: Provider) -> AsyncLLMClient:
    return AsyncLLMClient(provider)


# ─── success ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_success_yields_llm_candidates_and_evidence() -> None:
    lib = load_seed_patterns()
    candidates, meta = await propose_with_llm(
        _NL, lib, client=_client(TextProvider(_VALID)), model="gemma-2-27b", provider_name="local-27b"
    )
    assert candidates and all(c.source == "llm" for c in candidates)
    top = candidates[0]
    assert top.pattern_id == _PID
    assert top.slot_fills["goal_predicate"] == "queue_drained"
    assert top.missing_slots == []
    assert meta["source"] == "llm" and meta["fallback"] is None
    assert meta["evidence"]["provider"] == "local-27b"
    assert meta["evidence"]["model"] == "gemma-2-27b"
    assert meta["evidence"]["prompt_hash"] and meta["evidence"]["response_hash"]
    assert meta["evidence_hash"]


# ─── fallback triggers ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_on_schema_invalid() -> None:
    lib = load_seed_patterns()
    candidates, meta = await propose_with_llm(
        _NL, lib, client=_client(TextProvider("this is not json")), model="m"
    )
    assert meta["source"] == "rag_fallback"
    assert meta["fallback"] == "schema_invalid"
    assert all(c.source != "llm" for c in candidates)


@pytest.mark.asyncio
async def test_fallback_on_unknown_pattern_id() -> None:
    lib = load_seed_patterns()
    bad = json.dumps({"candidates": [{"pattern_id": "nope.not_a_pattern", "slot_fills": {}}]})
    _, meta = await propose_with_llm(_NL, lib, client=_client(TextProvider(bad)), model="m")
    assert meta["fallback"] == "schema_invalid"


@pytest.mark.asyncio
async def test_fallback_on_unknown_slot_key() -> None:
    lib = load_seed_patterns()
    bad = json.dumps({"candidates": [{"pattern_id": _PID, "slot_fills": {"bogus_slot": "x"}}]})
    _, meta = await propose_with_llm(_NL, lib, client=_client(TextProvider(bad)), model="m")
    assert meta["fallback"] == "schema_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,trigger", [("PROVIDER_ERROR", "unreachable"), ("RUN_TIMEOUT", "timeout"), ("RATE_LIMIT", "rate_limit")])
async def test_fallback_on_provider_error(code, trigger) -> None:
    lib = load_seed_patterns()
    candidates, meta = await propose_with_llm(_NL, lib, client=_client(ErrorProvider(code)), model="m")
    assert meta["source"] == "rag_fallback"
    assert meta["fallback"] == trigger
    assert all(c.source != "llm" for c in candidates)


# ─── step run() integration (offline) ────────────────────────────────


@pytest.mark.asyncio
async def test_run_fake_provider_is_unchanged_baseline() -> None:
    out = await run({"nl_text": _NL, "provider": "fake"})
    assert out["status"] == "ok"
    assert out["source"] == "rag"
    assert out["evidence"] is None
    assert out["candidates"]


@pytest.mark.asyncio
async def test_run_openai_without_transport_degrades_to_rag() -> None:
    # provider=openai but no transport configured → OpenAIProvider raises
    # LlmError at call time → graceful RAG fallback, no network touched.
    out = await run({"nl_text": _NL, "provider": "openai", "model": "gpt"})
    assert out["status"] == "ok"
    assert out["source"] == "rag_fallback"
    assert out["fallback"] == "unreachable"
