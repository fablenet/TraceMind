from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class IntentTreeIssue:
    intent_id: str | None
    path: str
    message: str


def _extract_intents(payload: Mapping[str, Any]) -> Sequence[Any] | None:
    root_intents = payload.get("intents")
    if isinstance(root_intents, Sequence) and not isinstance(root_intents, (str, bytes, bytearray)):
        return root_intents
    spec = payload.get("spec")
    if isinstance(spec, Mapping):
        spec_intents = spec.get("intents")
        if isinstance(spec_intents, Sequence) and not isinstance(spec_intents, (str, bytes, bytearray)):
            return spec_intents
        embedded = spec.get("spec")
        if isinstance(embedded, Mapping):
            embedded_intents = embedded.get("intents")
            if isinstance(embedded_intents, Sequence) and not isinstance(embedded_intents, (str, bytes, bytearray)):
                return embedded_intents
    return None


def _extract_trace_links(intent: Mapping[str, Any], *, index: int) -> tuple[Mapping[str, Any], str]:
    direct = intent.get("trace_links")
    if isinstance(direct, Mapping):
        return direct, f"intents[{index}].trace_links"
    metadata = intent.get("metadata")
    if isinstance(metadata, Mapping):
        mlinks = metadata.get("trace_links")
        if isinstance(mlinks, Mapping):
            return mlinks, f"intents[{index}].metadata.trace_links"
    spec = intent.get("spec")
    if isinstance(spec, Mapping):
        slinks = spec.get("trace_links")
        if isinstance(slinks, Mapping):
            return slinks, f"intents[{index}].spec.trace_links"
    return {}, f"intents[{index}].trace_links"


def _has_success_criteria(intent: Mapping[str, Any]) -> bool:
    if "success_criteria" in intent:
        return True
    spec = intent.get("spec")
    return isinstance(spec, Mapping) and "success_criteria" in spec


def validate_intent_tree(payload: Mapping[str, Any]) -> list[IntentTreeIssue]:
    issues: list[IntentTreeIssue] = []
    raw_intents = _extract_intents(payload)
    if raw_intents is None:
        return [IntentTreeIssue(intent_id=None, path="intents", message="missing intents array")]
    intents: list[tuple[int, Mapping[str, Any]]] = []
    for index, item in enumerate(raw_intents):
        if not isinstance(item, Mapping):
            issues.append(
                IntentTreeIssue(
                    intent_id=None,
                    path=f"intents[{index}]",
                    message="intent entry must be an object",
                )
            )
            continue
        intents.append((index, item))

    id_to_rows: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, intent in intents:
        intent_id = intent.get("id")
        if isinstance(intent_id, str) and intent_id:
            id_to_rows.setdefault(intent_id, []).append((index, intent))
        else:
            issues.append(IntentTreeIssue(intent_id=None, path=f"intents[{index}].id", message="missing or invalid id"))

    for intent_id, rows in id_to_rows.items():
        if len(rows) > 1:
            for index, _ in rows:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"intents[{index}].id",
                        message=f"duplicate id '{intent_id}'",
                    )
                )

    existing_ids = set(id_to_rows.keys())
    parent_of: dict[str, str] = {}
    children_of: dict[str, set[str]] = {intent_id: set() for intent_id in existing_ids}

    for intent_id, rows in id_to_rows.items():
        if len(rows) != 1:
            continue
        index, intent = rows[0]
        links, link_path = _extract_trace_links(intent, index=index)
        parent = links.get("parent_intent")
        if parent is not None:
            if not isinstance(parent, str) or not parent:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"{link_path}.parent_intent",
                        message="parent_intent must be a non-empty string or null",
                    )
                )
            elif parent not in existing_ids:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"{link_path}.parent_intent",
                        message=f"parent_intent '{parent}' does not exist",
                    )
                )
            else:
                parent_of[intent_id] = parent
                children_of[parent].add(intent_id)

        related = links.get("related_intents", [])
        if not isinstance(related, Sequence) or isinstance(related, (str, bytes, bytearray)):
            issues.append(
                IntentTreeIssue(
                    intent_id=intent_id,
                    path=f"{link_path}.related_intents",
                    message="related_intents must be an array",
                )
            )
            continue

        for ridx, related_id in enumerate(related):
            if not isinstance(related_id, str) or not related_id:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"{link_path}.related_intents[{ridx}]",
                        message="related intent id must be a non-empty string",
                    )
                )
                continue
            if related_id == intent_id:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"{link_path}.related_intents[{ridx}]",
                        message="related_intents must not reference self",
                    )
                )
            elif related_id not in existing_ids:
                issues.append(
                    IntentTreeIssue(
                        intent_id=intent_id,
                        path=f"{link_path}.related_intents[{ridx}]",
                        message=f"related intent '{related_id}' does not exist",
                    )
                )

    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node_id: str) -> None:
        current = state.get(node_id, 0)
        if current == 2:
            return
        if current == 1:
            start = stack.index(node_id) if node_id in stack else 0
            cycle = stack[start:] + [node_id]
            issues.append(
                IntentTreeIssue(
                    intent_id=node_id,
                    path="intents[*].trace_links.parent_intent",
                    message=f"parent_intent cycle detected: {' -> '.join(cycle)}",
                )
            )
            return
        state[node_id] = 1
        stack.append(node_id)
        parent = parent_of.get(node_id)
        if parent is not None:
            dfs(parent)
        stack.pop()
        state[node_id] = 2

    for node_id in sorted(existing_ids):
        dfs(node_id)

    for intent_id, rows in id_to_rows.items():
        if len(rows) != 1:
            continue
        if children_of[intent_id]:
            continue
        index, intent = rows[0]
        if _has_success_criteria(intent):
            continue
        issues.append(
            IntentTreeIssue(
                intent_id=intent_id,
                path=f"intents[{index}].success_criteria",
                message="leaf intent must define success_criteria",
            )
        )

    return sorted(
        issues,
        key=lambda item: (
            item.path,
            item.intent_id or "",
            item.message,
        ),
    )


__all__ = ["IntentTreeIssue", "validate_intent_tree"]
