"""Zero-token foundation for Stage 7-1: evidence / cache / fallback.

Everything here runs with the deterministic FakeProvider or tiny stub providers
that raise injected ``LlmError``s — **no network, no tokens, no GPU**. This is
the bulk of provider productization, validated entirely offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.ai.fallback import FallbackProvider, FallbackTrigger
from tm.ai.llm_cache import CacheMiss, CachingProvider, cache_key
from tm.ai.llm_evidence import LLM_CALL_EVENT, LlmEvidence, text_hash
from tm.ai.providers.base import LlmCallResult, LlmError, LlmUsage, Provider
from tm.ai.providers.fake import FakeProvider


# ─── stub providers (offline) ───────────────────────────────────────


class CountingProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, model, prompt, temperature=None, top_p=None, timeout_s=None):
        self.calls += 1
        return LlmCallResult(
            output_text=f"out:{self.calls}",
            usage=LlmUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
            raw={"n": self.calls},
        )


class ErrorProvider(Provider):
    def __init__(self, code) -> None:
        self.code = code
        self.calls = 0

    async def complete(self, *, model, prompt, temperature=None, top_p=None, timeout_s=None):
        self.calls += 1
        raise LlmError(self.code, f"injected {self.code}")


# ─── 7-1.1 evidence ─────────────────────────────────────────────────


def test_evidence_hashes_and_tokens() -> None:
    usage = LlmUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_usd=0.01)
    ev = LlmEvidence.from_call(
        provider="local-27b", model="gemma-2-27b", prompt="P", response_text="R", usage=usage
    )
    assert ev.prompt_hash == text_hash("P")
    assert ev.response_hash == text_hash("R")
    assert (ev.prompt_tokens, ev.completion_tokens, ev.total_tokens) == (10, 20, 30)
    assert ev.cost_usd == 0.01
    assert ev.fallback_from is None


def test_evidence_record_hash_deterministic() -> None:
    kw = dict(provider="p", model="m", prompt="hello", response_text="world")
    assert LlmEvidence.from_call(**kw).record_hash() == LlmEvidence.from_call(**kw).record_hash()
    assert LlmEvidence.from_call(**kw).record_hash() != LlmEvidence.from_call(
        provider="p", model="m", prompt="hello", response_text="WORLD"
    ).record_hash()


def test_evidence_to_evidence_entry() -> None:
    ev = LlmEvidence.from_call(
        provider="openai", model="gpt", prompt="P", response_text="R", fallback_from="timeout"
    )
    entry = ev.to_evidence_entry(source="ai.propose")
    assert entry.event_type == LLM_CALL_EVENT
    assert entry.source == "ai.propose"
    assert entry.data["prompt_hash"] == text_hash("P")
    assert entry.data["fallback_from"] == "timeout"
    assert entry.data["tokens"] == {"prompt": 0, "completion": 0, "total": 0}


# ─── 7-1.2 cache (replay = 0 token) ──────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_skips_inner_call(tmp_path: Path) -> None:
    inner = CountingProvider()
    cache = CachingProvider(inner, tmp_path / "c", provider_name="local-27b")
    first = await cache.complete(model="m", prompt="hi", temperature=0.0)
    assert inner.calls == 1 and cache.stats.misses == 1
    second = await cache.complete(model="m", prompt="hi", temperature=0.0)
    assert inner.calls == 1, "cache hit must not call the wrapped provider"
    assert cache.stats.hits == 1
    assert second.output_text == first.output_text  # served from disk


@pytest.mark.asyncio
async def test_cache_key_separates_model_and_params(tmp_path: Path) -> None:
    inner = CountingProvider()
    cache = CachingProvider(inner, tmp_path / "c", provider_name="p")
    await cache.complete(model="m1", prompt="hi", temperature=0.0)
    await cache.complete(model="m2", prompt="hi", temperature=0.0)  # different model
    await cache.complete(model="m1", prompt="hi", temperature=0.7)  # different temp
    assert inner.calls == 3, "distinct model/params must be distinct cache entries"


@pytest.mark.asyncio
async def test_cache_read_only_replay_is_token_free(tmp_path: Path) -> None:
    inner = CountingProvider()
    warm = CachingProvider(inner, tmp_path / "c", provider_name="p")
    await warm.complete(model="m", prompt="hi", temperature=0.0)
    # replay against the populated cache, refusing any live call
    replay = CachingProvider(ErrorProvider("PROVIDER_ERROR"), tmp_path / "c", provider_name="p", read_only=True)
    got = await replay.complete(model="m", prompt="hi", temperature=0.0)
    assert got.output_text == "out:1" and replay.stats.hits == 1
    with pytest.raises(CacheMiss):
        await replay.complete(model="m", prompt="MISS", temperature=0.0)


def test_cache_key_stable() -> None:
    a = cache_key(provider="p", model="m", prompt="x", temperature=0.0, top_p=None)
    b = cache_key(provider="p", model="m", prompt="x", temperature=0.0, top_p=None)
    assert a == b


# ─── 7-1.3 fallback (4 triggers) ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,trigger",
    [
        ("PROVIDER_ERROR", FallbackTrigger.UNREACHABLE),
        ("RUN_TIMEOUT", FallbackTrigger.TIMEOUT),
        ("RATE_LIMIT", FallbackTrigger.RATE_LIMIT),
    ],
)
async def test_fallback_degrades_and_records_trigger(code, trigger) -> None:
    primary = ErrorProvider(code)
    fb = FallbackProvider(primary, FakeProvider(), primary_name="local-27b", fallback_name="fake")
    result = await fb.complete(model="m", prompt="hello")
    assert result.output_text.startswith("echo[m]")  # served by fake
    assert fb.last_event is not None
    assert fb.last_event.trigger is trigger
    assert fb.last_event.from_provider == "local-27b"


@pytest.mark.asyncio
async def test_fallback_passthrough_on_success() -> None:
    fb = FallbackProvider(CountingProvider(), FakeProvider())
    result = await fb.complete(model="m", prompt="hi")
    assert result.output_text == "out:1"
    assert fb.last_event is None


@pytest.mark.asyncio
async def test_fallback_does_not_swallow_bad_request() -> None:
    fb = FallbackProvider(ErrorProvider("BAD_REQUEST"), FakeProvider())
    with pytest.raises(LlmError) as ei:
        await fb.complete(model="m", prompt="hi")
    assert ei.value.code == "BAD_REQUEST"
    assert fb.last_event is None
