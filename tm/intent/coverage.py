from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.policy.deterministic import canonical_json_bytes
from tm.utils.yaml import import_yaml

yaml = import_yaml()
_SUFFIXES = {".json", ".yaml", ".yml"}


@dataclass(frozen=True)
class IntentCoverageOutcome:
    exit_code: int
    report: Mapping[str, Any]


def _load_mapping(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML is required to read YAML files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return payload


def _extract_intent_nodes(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    intents = payload.get("intents")
    if isinstance(intents, Sequence) and not isinstance(intents, (str, bytes, bytearray)):
        return [item for item in intents if isinstance(item, Mapping)]
    spec = payload.get("spec")
    if isinstance(spec, Mapping):
        spec_intents = spec.get("intents")
        if isinstance(spec_intents, Sequence) and not isinstance(spec_intents, (str, bytes, bytearray)):
            return [item for item in spec_intents if isinstance(item, Mapping)]
        embedded = spec.get("spec")
        if isinstance(embedded, Mapping):
            embedded_intents = embedded.get("intents")
            if isinstance(embedded_intents, Sequence) and not isinstance(embedded_intents, (str, bytes, bytearray)):
                return [item for item in embedded_intents if isinstance(item, Mapping)]
    return []


def _extract_parent_intent(intent: Mapping[str, Any]) -> str | None:
    links = intent.get("trace_links")
    if isinstance(links, Mapping):
        parent = links.get("parent_intent")
        if isinstance(parent, str) and parent:
            return parent
    metadata = intent.get("metadata")
    if isinstance(metadata, Mapping):
        mlinks = metadata.get("trace_links")
        if isinstance(mlinks, Mapping):
            parent = mlinks.get("parent_intent")
            if isinstance(parent, str) and parent:
                return parent
    spec = intent.get("spec")
    if isinstance(spec, Mapping):
        slinks = spec.get("trace_links")
        if isinstance(slinks, Mapping):
            parent = slinks.get("parent_intent")
            if isinstance(parent, str) and parent:
                return parent
    return None


def _suite_paths(path: Path) -> list[Path]:
    resolved = path.expanduser()
    if resolved.is_file():
        return [resolved]
    if not resolved.is_dir():
        raise ValueError(f"{resolved}: not a file or directory")
    return sorted(item for item in resolved.rglob("*") if item.is_file() and item.suffix.lower() in _SUFFIXES)


def _extract_tests(payload: Mapping[str, Any], *, path: Path) -> list[tuple[str, list[str]]]:
    kind = payload.get("kind")
    if kind not in {None, "TestSuite"}:
        return []
    spec = payload.get("spec")
    if not isinstance(spec, Mapping):
        return []
    tests = spec.get("tests")
    if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes, bytearray)):
        return []
    rows: list[tuple[str, list[str]]] = []
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            continue
        test_id_raw = test.get("id")
        test_id = str(test_id_raw) if isinstance(test_id_raw, str) and test_id_raw else f"{path.name}#test-{index}"
        refs_raw = test.get("intent_refs")
        refs: list[str] = []
        if isinstance(refs_raw, Sequence) and not isinstance(refs_raw, (str, bytes, bytearray)):
            refs = [item for item in refs_raw if isinstance(item, str) and item]
        rows.append((test_id, refs))
    return rows


def _extract_policy_rule_refs(payload: Mapping[str, Any]) -> tuple[int, int, list[str]]:
    rules = payload.get("rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes, bytearray)):
        return 0, 0, []
    total = 0
    with_refs = 0
    refs: list[str] = []
    for raw in rules:
        if not isinstance(raw, Mapping):
            continue
        total += 1
        intent_refs = raw.get("intent_refs")
        if not isinstance(intent_refs, Sequence) or isinstance(intent_refs, (str, bytes, bytearray)):
            continue
        current = [item for item in intent_refs if isinstance(item, str) and item]
        if not current:
            continue
        with_refs += 1
        refs.extend(current)
    return total, with_refs, refs


def compute_intents_coverage(
    *,
    intents_path: Path,
    tests_path: Path,
    policy_path: Path | None = None,
) -> IntentCoverageOutcome:
    intents_payload = _load_mapping(intents_path.expanduser())
    nodes = _extract_intent_nodes(intents_payload)
    if not nodes:
        raise ValueError("intent tree missing intents[]")

    intent_ids: list[str] = []
    parent_ids: set[str] = set()
    for node in nodes:
        intent_id = node.get("id")
        if not isinstance(intent_id, str) or not intent_id:
            continue
        intent_ids.append(intent_id)
        parent = _extract_parent_intent(node)
        if parent:
            parent_ids.add(parent)
    existing_ids = set(intent_ids)
    leaf_ids = sorted(intent_id for intent_id in existing_ids if intent_id not in parent_ids)

    suites: list[tuple[str, list[str]]] = []
    for path in _suite_paths(tests_path):
        payload = _load_mapping(path)
        suites.extend(_extract_tests(payload, path=path))

    if not suites:
        raise ValueError("no TestSuite tests found")

    covered_by_tests: set[str] = set()
    tests_without_intents: set[str] = set()
    for test_id, refs in suites:
        has_missing = False
        for ref in refs:
            if ref in existing_ids:
                covered_by_tests.add(ref)
            else:
                has_missing = True
        if has_missing:
            tests_without_intents.add(test_id)

    intents_without_tests = sorted(intent_id for intent_id in existing_ids if intent_id not in covered_by_tests)
    uncovered_leaf_intents = sorted(intent_id for intent_id in leaf_ids if intent_id not in covered_by_tests)

    warnings: list[str] = []
    non_leaf_uncovered = sorted(intent_id for intent_id in intents_without_tests if intent_id not in set(leaf_ids))
    if non_leaf_uncovered:
        warnings.append(f"non_leaf_intents_without_tests: {','.join(non_leaf_uncovered)}")

    policy_rules_scanned = 0
    policy_rules_with_intent_refs = 0
    policy_unknown_intent_refs: list[str] = []
    if policy_path is not None:
        policy_payload = _load_mapping(policy_path.expanduser())
        policy_rules_scanned, policy_rules_with_intent_refs, policy_refs = _extract_policy_rule_refs(policy_payload)
        policy_unknown_intent_refs = sorted({ref for ref in policy_refs if ref not in existing_ids})
        if policy_unknown_intent_refs:
            warnings.append(f"policy_unknown_intent_refs: {','.join(policy_unknown_intent_refs)}")

    report: dict[str, Any] = {
        "uncovered_leaf_intents": uncovered_leaf_intents,
        "intents_without_tests": intents_without_tests,
        "tests_without_intents": sorted(tests_without_intents),
        "warnings": warnings,
        "summary": {
            "total_intents": len(existing_ids),
            "leaf_intents": len(leaf_ids),
            "tests_scanned": len(suites),
            "tests_without_intents": len(tests_without_intents),
            "intents_without_tests": len(intents_without_tests),
            "uncovered_leaf_intents": len(uncovered_leaf_intents),
            "policy_rules_scanned": policy_rules_scanned,
            "policy_rules_with_intent_refs": policy_rules_with_intent_refs,
            "policy_unknown_intent_refs": len(policy_unknown_intent_refs),
            "warnings": len(warnings),
        },
    }
    return IntentCoverageOutcome(
        exit_code=1 if uncovered_leaf_intents else 0,
        report=json.loads(canonical_json_bytes(report).decode("utf-8")),
    )


__all__ = ["IntentCoverageOutcome", "compute_intents_coverage"]
