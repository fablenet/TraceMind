from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource


@dataclass(frozen=True)
class AstValidationIssue:
    json_path: str
    message: str


_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "v0.1"
_ENVELOPE_SCHEMA_PATH = _SCHEMA_ROOT / "envelope.schema.json"
_KIND_SCHEMA_FILES: dict[str, str] = {
    "Candidate": "candidate.schema.json",
    "Proposal": "proposal.schema.json",
    "Patch": "patch.schema.json",
    "TestSuite": "testsuite.schema.json",
    "RegistryEntry": "registry_entry.schema.json",
    "ConsistencyReport": "consistency_report.schema.json",
}
SUPPORTED_CANONICAL_KINDS: tuple[str, ...] = tuple(_KIND_SCHEMA_FILES.keys())
_SCHEMA_REGISTRY: Registry = Registry()


def _load_schema(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    schema_id = schema.get("$id") or path.as_uri()
    schema.setdefault("$id", schema_id)
    global _SCHEMA_REGISTRY
    _SCHEMA_REGISTRY = _SCHEMA_REGISTRY.with_resource(schema_id, Resource.from_contents(schema))
    return schema


_FORMAT_CHECKER = FormatChecker()
_ENVELOPE_SCHEMA = _load_schema(_ENVELOPE_SCHEMA_PATH)
_KIND_SCHEMAS: dict[str, Mapping[str, Any]] = {
    kind: _load_schema(_SCHEMA_ROOT / "kinds" / filename) for kind, filename in _KIND_SCHEMA_FILES.items()
}


def _json_path(path_parts: Iterable[Any]) -> str:
    path = "$"
    for part in path_parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            key = str(part)
            if key.isidentifier():
                path += f".{key}"
            else:
                path += f"['{key}']"
    return path


def _format_error(error: ValidationError) -> AstValidationIssue:
    return AstValidationIssue(json_path=_json_path(error.path), message=error.message)


def _iter_schema_errors(schema: Mapping[str, Any], payload: Mapping[str, Any]) -> Sequence[AstValidationIssue]:
    validator = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER, registry=_SCHEMA_REGISTRY)
    errors = sorted(validator.iter_errors(payload), key=lambda err: tuple(err.path))
    return tuple(_format_error(error) for error in errors)


def validate_canonical_ast(payload: Mapping[str, Any]) -> Sequence[AstValidationIssue]:
    if not isinstance(payload, Mapping):
        return (AstValidationIssue(json_path="$", message="document must be an object"),)
    kind = payload.get("kind")
    schema = _KIND_SCHEMAS.get(kind) if isinstance(kind, str) else None
    if schema is None:
        issues = list(_iter_schema_errors(_ENVELOPE_SCHEMA, payload))
        if not isinstance(kind, str):
            issues.append(AstValidationIssue(json_path="$.kind", message="kind must be a string"))
        else:
            issues.append(AstValidationIssue(json_path="$.kind", message=f"unsupported canonical AST kind '{kind}'"))
        return tuple(issues)
    return _iter_schema_errors(schema, payload)


__all__ = ["AstValidationIssue", "SUPPORTED_CANONICAL_KINDS", "validate_canonical_ast"]
