from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.utils.yaml import import_yaml

yaml = import_yaml()
_RISK_ENUM = {"low", "medium", "high"}


def _extract_intent_ids(intent_tree: Mapping[str, Any]) -> set[str]:
    nodes = intent_tree.get("intents")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
        spec = intent_tree.get("spec")
        if isinstance(spec, Mapping):
            nodes = spec.get("intents")
            if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
                embedded = spec.get("spec")
                if isinstance(embedded, Mapping):
                    nodes = embedded.get("intents")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
        return set()
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            ids.add(node_id)
    return ids


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _collect_refs(spec: Mapping[str, Any], *, list_key: str, obj_key: str, path_prefix: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    raw_list = spec.get(list_key)
    if isinstance(raw_list, Sequence) and not isinstance(raw_list, (str, bytes, bytearray)):
        for idx, ref in enumerate(raw_list):
            if isinstance(ref, str) and ref:
                refs.append((ref, f"{path_prefix}.{list_key}[{idx}]"))
    raw_objs = spec.get(obj_key)
    if isinstance(raw_objs, Sequence) and not isinstance(raw_objs, (str, bytes, bytearray)):
        for idx, item in enumerate(raw_objs):
            if not isinstance(item, Mapping):
                continue
            ref = item.get("ref")
            if isinstance(ref, str) and ref:
                refs.append((ref, f"{path_prefix}.{obj_key}[{idx}].ref"))
    return refs


def _resolve_ref_path(ref: str, base_paths: Sequence[Path]) -> Path | None:
    candidate = Path(ref).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for base in base_paths:
        resolved = (base / ref).expanduser()
        if resolved.exists():
            return resolved
    return None


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


def _sort_issues(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(items, key=lambda row: (row["code"], row["path"], row["message"]))


def validate_proposal(
    proposal: Mapping[str, Any],
    intent_tree: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    base_paths: Sequence[Path] | None = None,
) -> Mapping[str, Any]:
    del policy  # reserved for future lint rules
    roots = [Path.cwd()] if not base_paths else [Path(p).expanduser() for p in base_paths]
    intent_ids = _extract_intent_ids(intent_tree)
    spec = proposal.get("spec")
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(spec, Mapping):
        errors.append(
            {
                "code": "PROPOSAL_SPEC_INVALID",
                "path": "spec",
                "message": "spec must be an object",
            }
        )
        return {"summary": {"errors": 1, "warnings": 0}, "errors": errors, "warnings": warnings}

    impacted = _coerce_string_list(spec.get("impacted_intents"))
    if not impacted:
        errors.append(
            {
                "code": "PROPOSAL_IMPACTED_EMPTY",
                "path": "spec.impacted_intents",
                "message": "impacted_intents must be a non-empty array",
            }
        )
    for idx, intent_id in enumerate(impacted):
        if intent_id not in intent_ids:
            errors.append(
                {
                    "code": "PROPOSAL_IMPACTED_UNKNOWN_INTENT",
                    "path": f"spec.impacted_intents[{idx}]",
                    "message": f"intent '{intent_id}' does not exist in intent tree",
                }
            )
    impacted_set = set(impacted)

    patch_refs = _collect_refs(spec, list_key="patch_refs", obj_key="patches", path_prefix="spec")
    if not patch_refs:
        errors.append(
            {
                "code": "PROPOSAL_PATCH_REF_EMPTY",
                "path": "spec.patch_refs",
                "message": "proposal must include patch_refs or patches[*].ref",
            }
        )
    for ref, path in patch_refs:
        if _resolve_ref_path(ref, roots) is None:
            errors.append(
                {
                    "code": "PROPOSAL_PATCH_REF_MISSING",
                    "path": path,
                    "message": f"patch ref '{ref}' not found",
                }
            )

    testsuite_refs = _collect_refs(spec, list_key="testsuite_refs", obj_key="tests", path_prefix="spec")
    if not testsuite_refs:
        errors.append(
            {
                "code": "PROPOSAL_TEST_REF_EMPTY",
                "path": "spec.testsuite_refs",
                "message": "proposal must include testsuite_refs or tests[*].ref",
            }
        )

    for idx, (ref, path) in enumerate(testsuite_refs):
        resolved = _resolve_ref_path(ref, roots)
        if resolved is None:
            errors.append(
                {
                    "code": "PROPOSAL_TEST_REF_MISSING",
                    "path": path,
                    "message": f"testsuite ref '{ref}' not found",
                }
            )
            continue
        try:
            testsuite = _load_mapping(resolved)
        except Exception as exc:
            errors.append(
                {
                    "code": "PROPOSAL_TEST_REF_INVALID",
                    "path": path,
                    "message": f"failed to parse testsuite ref '{ref}': {exc}",
                }
            )
            continue
        ts_spec = testsuite.get("spec")
        if not isinstance(ts_spec, Mapping):
            errors.append(
                {
                    "code": "PROPOSAL_TESTSUITE_INVALID",
                    "path": f"{path}#spec",
                    "message": f"testsuite '{ref}' missing spec object",
                }
            )
            continue

        suite_intent_refs = _coerce_string_list(ts_spec.get("intent_refs"))
        for sid, suite_ref in enumerate(suite_intent_refs):
            if suite_ref not in intent_ids:
                errors.append(
                    {
                        "code": "PROPOSAL_TEST_INTENT_UNKNOWN",
                        "path": f"{path}#spec.intent_refs[{sid}]",
                        "message": f"testsuite intent '{suite_ref}' does not exist in intent tree",
                    }
                )
            if impacted_set and suite_ref not in impacted_set:
                errors.append(
                    {
                        "code": "PROPOSAL_TEST_INTENT_OUTSIDE_IMPACT",
                        "path": f"{path}#spec.intent_refs[{sid}]",
                        "message": f"testsuite intent '{suite_ref}' not in impacted_intents",
                    }
                )

        tests = ts_spec.get("tests")
        if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes, bytearray)):
            continue
        for tidx, test in enumerate(tests):
            if not isinstance(test, Mapping):
                continue
            test_refs = _coerce_string_list(test.get("intent_refs"))
            for ridx, tref in enumerate(test_refs):
                if tref not in intent_ids:
                    errors.append(
                        {
                            "code": "PROPOSAL_TEST_INTENT_UNKNOWN",
                            "path": f"{path}#spec.tests[{tidx}].intent_refs[{ridx}]",
                            "message": f"test intent '{tref}' does not exist in intent tree",
                        }
                    )
                if impacted_set and tref not in impacted_set:
                    errors.append(
                        {
                            "code": "PROPOSAL_TEST_INTENT_OUTSIDE_IMPACT",
                            "path": f"{path}#spec.tests[{tidx}].intent_refs[{ridx}]",
                            "message": f"test intent '{tref}' not in impacted_intents",
                        }
                    )

    risk = spec.get("risk")
    if not isinstance(risk, str) or risk not in _RISK_ENUM:
        errors.append(
            {
                "code": "PROPOSAL_RISK_INVALID",
                "path": "spec.risk",
                "message": "risk must be one of: low, medium, high",
            }
        )

    errors = _sort_issues(errors)
    warnings = _sort_issues(warnings)
    return {
        "summary": {"errors": len(errors), "warnings": len(warnings)},
        "errors": errors,
        "warnings": warnings,
    }


__all__ = ["validate_proposal"]
