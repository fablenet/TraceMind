from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.utils.yaml import import_yaml

from tm.artifacts import ArtifactValidationError, validate_policy_spec
from tm.caps.catalog import DEFAULT_CATALOG_PATH
from tm.caps import CapabilityCatalog
from tm.intent import intent_precheck, validate_intent

yaml = import_yaml()
ILLEGAL_INTENT_KEYS = {
    "steps",
    "rules",
    "capability_id",
    "workflows",
    "workflow",
    "flow",
    "execution",
    "plan",
    "order",
}


def _load_structured(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return payload


def _load_catalog_specs(path: Path | None) -> list[Mapping[str, Any]]:
    catalog = CapabilityCatalog(path=path or DEFAULT_CATALOG_PATH)
    return [entry.spec for entry in catalog.list()]


def _contains_illegal_key(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = key.strip().lower()
            if normalized in ILLEGAL_INTENT_KEYS:
                return f"prohibited key '{key}'"
            found = _contains_illegal_key(value)
            if found:
                return found
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            found = _contains_illegal_key(item)
            if found:
                return found
    return None


def evaluate_intent_status(
    intent: Mapping[str, Any],
    policy: Mapping[str, Any],
    capabilities: Sequence[Mapping[str, Any]],
) -> tuple[str, str | None, Mapping[str, Any] | None]:
    try:
        validate_intent(intent)
    except ArtifactValidationError as exc:
        return "ILLEGAL", str(exc), None

    try:
        validate_policy_spec(policy)
    except ArtifactValidationError as exc:
        return "ILLEGAL", f"policy invalid: {exc}", None

    illegal_reason = _contains_illegal_key(intent)
    if illegal_reason:
        return "ILLEGAL", illegal_reason, None

    precheck = intent_precheck(intent, policy=policy, capabilities=capabilities)
    status_map = {
        "valid": "OK",
        "underconstrained": "UNDERCONSTRAINED",
        "overconstrained": "OVERCONSTRAINED",
    }
    result_status = status_map.get(precheck.status)
    if result_status:
        return result_status, precheck.reason, dict(precheck.details or {})
    return "ILLEGAL", precheck.reason, dict(precheck.details or {})


def cmd_intent_validate(args: argparse.Namespace) -> int:
    try:
        intent = _load_structured(Path(args.intent))
        policy = _load_structured(Path(args.policy))
    except Exception as exc:
        print(f"intent validate: failed to load input: {exc}", file=sys.stderr)
        raise SystemExit(1)

    catalog_path = Path(args.catalog) if args.catalog else DEFAULT_CATALOG_PATH
    capabilities = _load_catalog_specs(catalog_path)

    status, reason, details = evaluate_intent_status(intent, policy, capabilities)
    payload: dict[str, Any] = {"status": status}
    if reason:
        payload["reason"] = reason
    if details:
        payload["details"] = details

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(status)
        if reason:
            print(reason)
        if args.verbose and details:
            print(json.dumps(details, indent=2, ensure_ascii=False))

    return 0 if status == "OK" else 1


def register_intent_commands(subparsers: argparse._SubParsersAction) -> None:
    intent_parser = subparsers.add_parser("intent", help="intent validation tools")
    intent_sub = intent_parser.add_subparsers(dest="intent_cmd")
    intent_sub.required = True

    from tm.cli.intent_chat import register_session_commands

    register_session_commands(intent_sub)

    validate_parser = intent_sub.add_parser("validate", help="validate an IntentSpec using catalog + policy")
    validate_parser.add_argument("intent", help="Intent YAML/JSON path")
    validate_parser.add_argument("--policy", required=True, help="PolicySpec YAML/JSON path")
    validate_parser.add_argument(
        "--catalog",
        help="Capability catalog JSON file (default: ~/.trace-mind/capabilities.json)",
    )
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-friendly JSON output with status/reason/details",
    )
    validate_parser.add_argument(
        "--verbose",
        action="store_true",
        help="show detailed findings even when not emitting JSON",
    )
    validate_parser.set_defaults(func=cmd_intent_validate)


__all__ = [
    "evaluate_intent_status",
    "register_intent_commands",
]
