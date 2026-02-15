from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

SET_LIKE_PATHS: set[str] = {
    "metadata.trace_links.related_intents",
    "spec.trace_links.related_intents",
    "spec.intent_refs",
    "spec.impacted_intents",
    "spec.tests[*].intent_refs",
}


def _parse_path(path: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for part in path.split("."):
        if part.endswith("[*]"):
            key = part[:-3]
            if key:
                tokens.append(key)
            tokens.append("*")
            continue
        if part == "[*]":
            tokens.append("*")
            continue
        tokens.append(part)
    return tuple(tokens)


_SET_LIKE_TOKENS = {_parse_path(path) for path in SET_LIKE_PATHS}


def _normalize_path(path: str | Sequence[str | int]) -> tuple[str, ...]:
    if isinstance(path, str):
        return _parse_path(path)
    tokens: list[str] = []
    for segment in path:
        if isinstance(segment, int):
            tokens.append("*")
        else:
            tokens.append(segment)
    return tuple(tokens)


def is_set_like_field(path: str | Sequence[str | int]) -> bool:
    return _normalize_path(path) in _SET_LIKE_TOKENS


def _compact_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _sort_key_for_set_like_item(value: Any) -> tuple[int, str]:
    is_scalar = value is None or isinstance(value, (str, int, float, bool))
    if is_scalar:
        return (0, _compact_dumps(value))
    return (1, _compact_dumps(value))


def _canonicalize_set_like_list(items: Sequence[Any]) -> list[Any]:
    seen: set[tuple[int, str]] = set()
    deduped: list[tuple[tuple[int, str], Any]] = []
    for item in items:
        key = _sort_key_for_set_like_item(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((key, item))
    deduped.sort(key=lambda entry: entry[0])
    return [item for _, item in deduped]


def canonicalize(obj: Any, *, _path: tuple[str, ...] = ()) -> Any:
    if isinstance(obj, Mapping):
        items = sorted(((str(key), value) for key, value in obj.items()), key=lambda item: item[0])
        return {key: canonicalize(value, _path=_path + (key,)) for key, value in items}

    if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        canonical_items = [canonicalize(item, _path=_path + ("*",)) for item in obj]
        if is_set_like_field(_path):
            return _canonicalize_set_like_list(canonical_items)
        return canonical_items

    return obj


def canonical_dumps(obj: Any) -> str:
    canonical_obj = canonicalize(obj)
    text = json.dumps(canonical_obj, indent=2, ensure_ascii=False, sort_keys=False, separators=(",", ": "))
    return text if text.endswith("\n") else f"{text}\n"


__all__ = ["SET_LIKE_PATHS", "canonicalize", "canonical_dumps", "is_set_like_field"]
