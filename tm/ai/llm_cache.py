"""prompt_hash response cache — Phase 7 Stage 7-1.2.

A :class:`CachingProvider` wraps **any** :class:`~tm.ai.providers.base.Provider`
and persists each completion to disk keyed by a stable hash of
``(provider, model, prompt, temperature, top_p)``. On a cache hit the wrapped
provider is **not** called — so re-running a golden-set evaluation (or a flaky
test) after the first pass costs **zero tokens**. This is the central cost lever
for a token-expensive personal setup: the first local-27B / API pass populates
the cache; every replay thereafter is free and offline.

Deterministic and side-effect-isolated (one JSON file per key under
``cache_dir``); safe to commit a populated cache for reproducible CI replays.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from tm.ai.providers.base import LlmCallResult, LlmUsage, Provider
from tm.artifacts.hash import body_hash


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def calls(self) -> int:
        return self.hits + self.misses


def cache_key(
    *,
    provider: str,
    model: str,
    prompt: str,
    temperature: Optional[float],
    top_p: Optional[float],
) -> str:
    """Stable key for a completion request (token-affecting fields only)."""
    return body_hash(
        {
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "temperature": temperature,
            "top_p": top_p,
        }
    )


class CachingProvider(Provider):
    """Disk-backed read-through cache around another provider.

    ``provider_name`` identifies the *underlying* endpoint (e.g. ``"openai"`` or
    ``"local-27b"``) so distinct models / endpoints never collide in the cache.
    ``read_only`` (replay) refuses to call the wrapped provider on a miss,
    raising :class:`CacheMiss` — useful to assert an eval is fully replayable
    offline / token-free.
    """

    def __init__(
        self,
        inner: Provider,
        cache_dir: str | Path,
        *,
        provider_name: str = "llm",
        read_only: bool = False,
    ) -> None:
        self._inner = inner
        self._dir = Path(cache_dir)
        self._provider_name = provider_name
        self._read_only = read_only
        self.stats = CacheStats()

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _load(self, key: str) -> LlmCallResult | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        u = data.get("usage", {})
        usage = LlmUsage(
            prompt_tokens=int(u.get("prompt_tokens", 0)),
            completion_tokens=int(u.get("completion_tokens", 0)),
            total_tokens=int(u.get("total_tokens", 0)),
            cost_usd=u.get("cost_usd"),
        )
        return LlmCallResult(output_text=data.get("output_text", ""), usage=usage, raw=data.get("raw"))

    def _store(self, key: str, result: LlmCallResult) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "output_text": result.output_text,
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "total_tokens": result.usage.total_tokens,
                "cost_usd": result.usage.cost_usd,
            },
            "raw": result.raw,
        }
        # canonical, stable on-disk bytes for reproducible / committable caches
        self._path(key).write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        temperature: float | None = None,
        top_p: float | None = None,
        timeout_s: float | None = None,
    ) -> LlmCallResult:
        key = cache_key(
            provider=self._provider_name,
            model=model,
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
        )
        cached = self._load(key)
        if cached is not None:
            self.stats.hits += 1
            return cached
        if self._read_only:
            raise CacheMiss(key)
        self.stats.misses += 1
        result = await self._inner.complete(
            model=model, prompt=prompt, temperature=temperature, top_p=top_p, timeout_s=timeout_s
        )
        self._store(key, result)
        return result


class CacheMiss(Exception):
    """Raised by a ``read_only`` :class:`CachingProvider` on a cache miss."""

    def __init__(self, key: str) -> None:
        super().__init__(f"cache miss for key {key[:12]}… (read-only replay)")
        self.key = key


__all__ = ["CacheMiss", "CacheStats", "CachingProvider", "cache_key"]
