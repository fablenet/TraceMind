from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import sys

from tm.artifacts import (
    ArtifactValidationError,
    validate_capability_spec,
    validate_execution_trace,
    validate_integrated_state_report,
    validate_intent_spec,
    validate_patch_proposal,
    validate_policy_spec,
    validate_workflow_policy,
)
from tm.ast import AstValidationIssue, SUPPORTED_CANONICAL_KINDS, validate_canonical_ast
from tm.utils.yaml import import_yaml
from tm.validate import find_conflicts

yaml = import_yaml()
_AST_FILE_SUFFIXES = {".json", ".yaml", ".yml"}


def _expand(patterns: Sequence[str]) -> Sequence[Path]:
    seen: dict[Path, None] = {}
    for pattern in patterns:
        path = Path(pattern)
        matches: Iterable[Path]
        if path.exists():
            matches = [path]
        else:
            matches = (Path(p) for p in glob.glob(pattern, recursive=True))
        found = False
        for match in matches:
            if match.is_file():
                seen.setdefault(match.resolve(), None)
                found = True
        if not found:
            raise SystemExit(f"no files matched '{pattern}'")
    return tuple(sorted(seen.keys()))


def _load_yaml(path: Path) -> Mapping[str, object]:
    if yaml is None:
        raise SystemExit("PyYAML required; install with `pip install pyyaml`.")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
        if not isinstance(data, Mapping):
            raise SystemExit(f"{path}: expected mapping document")
        return data


def _load_structured(path: Path) -> Mapping[str, object]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML required; install with `pip install pyyaml`.")
        data = yaml.safe_load(text) or {}
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return data


@dataclass(frozen=True)
class _AstDocument:
    path: Path
    payload: Mapping[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class _AstResult:
    path: Path
    issues: Sequence[AstValidationIssue]


def _partition_artifact_arguments(arguments: Sequence[str]) -> tuple[list[str], list[Path]]:
    file_patterns: list[str] = []
    directories: list[Path] = []
    for arg in arguments:
        candidate = Path(arg)
        if candidate.exists() and candidate.is_dir():
            directories.append(candidate)
        else:
            file_patterns.append(arg)
    return file_patterns, directories


def _looks_like_ast_payload(payload: Mapping[str, Any]) -> bool:
    api_version = payload.get("apiVersion")
    if isinstance(api_version, str) and api_version.startswith("tracemind.io/"):
        return True
    kind = payload.get("kind")
    return isinstance(kind, str) and kind in SUPPORTED_CANONICAL_KINDS


def _make_ast_document(path: Path) -> _AstDocument:
    resolved = path.resolve()
    try:
        payload = _load_structured(resolved)
        return _AstDocument(path=resolved, payload=payload)
    except ValueError as exc:
        return _AstDocument(path=resolved, payload=None, error=str(exc))


def _find_ast_files(directory: Path) -> Sequence[Path]:
    resolved = directory.resolve()
    if not resolved.exists():
        raise SystemExit(f"{directory}: no such directory")
    if not resolved.is_dir():
        raise SystemExit(f"{directory}: not a directory")
    files = sorted(path for path in resolved.rglob("*") if path.is_file() and path.suffix.lower() in _AST_FILE_SUFFIXES)
    return tuple(files)


def _collect_ast_directories(directories: Sequence[Path]) -> Sequence[_AstDocument]:
    documents: list[_AstDocument] = []
    for directory in directories:
        files = _find_ast_files(directory)
        if not files:
            documents.append(
                _AstDocument(
                    path=directory.resolve(),
                    payload=None,
                    error=f"no canonical AST files found in {directory}",
                )
            )
            continue
        documents.extend(_make_ast_document(file_path) for file_path in files)
    return tuple(documents)


def _collect_ast_files(paths: Sequence[Path], *, force_ast: bool) -> tuple[Sequence[_AstDocument], Sequence[Path]]:
    ast_documents: list[_AstDocument] = []
    artifact_files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        treat_as_ast = force_ast
        payload: Mapping[str, Any] | None = None
        if not treat_as_ast:
            try:
                payload = _load_structured(resolved)
            except ValueError:
                payload = None
            else:
                if _looks_like_ast_payload(payload):
                    treat_as_ast = True
        if treat_as_ast:
            if payload is None:
                ast_documents.append(_make_ast_document(resolved))
            else:
                ast_documents.append(_AstDocument(path=resolved, payload=payload))
        else:
            artifact_files.append(resolved)
    return tuple(ast_documents), tuple(artifact_files)


def _validate_ast_documents(documents: Sequence[_AstDocument]) -> Sequence[_AstResult]:
    results: list[_AstResult] = []
    for document in documents:
        if document.payload is None:
            message = document.error or "failed to load canonical AST payload"
            issues: Sequence[AstValidationIssue] = (AstValidationIssue(json_path="$", message=message),)
        else:
            issues = validate_canonical_ast(document.payload)
        results.append(_AstResult(path=document.path, issues=issues))
    return tuple(results)


def _ast_results_to_json(results: Sequence[_AstResult]) -> Sequence[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for result in results:
        payload.append(
            {
                "file": str(result.path),
                "errors": [{"json_path": issue.json_path, "message": issue.message} for issue in result.issues],
            }
        )
    return tuple(payload)


def _print_ast_results(results: Sequence[_AstResult]) -> None:
    for result in results:
        if result.issues:
            print(f"{result.path}: canonical AST invalid", file=sys.stderr)
            for issue in result.issues:
                print(f"  - {issue.json_path}: {issue.message}", file=sys.stderr)
        else:
            print(f"{result.path}: canonical AST valid")


def _validate_artifact_files(paths: Sequence[Path], schema_name: str | None) -> bool:
    has_errors = False
    for artifact_path in paths:
        try:
            _validate_artifact_file(artifact_path, schema_name)
            print(f"{artifact_path}: valid")
        except (ArtifactValidationError, RuntimeError, ValueError) as exc:
            print(f"{artifact_path}: {exc}", file=sys.stderr)
            has_errors = True
    return has_errors


ARTIFACT_VALIDATORS = {
    "IntentSpec": validate_intent_spec,
    "PolicySpec": validate_policy_spec,
    "CapabilitySpec": validate_capability_spec,
    "WorkflowPolicy": validate_workflow_policy,
    "ExecutionTrace": validate_execution_trace,
    "IntegratedStateReport": validate_integrated_state_report,
    "PatchProposal": validate_patch_proposal,
}


def _infer_artifact_schema(payload: Mapping[str, object]) -> str:
    if "proposal_id" in payload:
        return "PatchProposal"
    if "trace_id" in payload:
        return "ExecutionTrace"
    if "report_id" in payload:
        return "IntegratedStateReport"
    if "workflow_id" in payload and "steps" in payload:
        return "WorkflowPolicy"
    if "capability_id" in payload and "event_types" in payload:
        return "CapabilitySpec"
    if "policy_id" in payload and "state_schema" in payload:
        return "PolicySpec"
    if "intent_id" in payload and "goal" in payload:
        return "IntentSpec"
    raise RuntimeError("unable to infer artifact schema")


def _validate_artifact_file(path: Path, schema_name: str | None) -> None:
    payload = _load_structured(path)
    if schema_name is None:
        schema_name = _infer_artifact_schema(payload)
    if schema_name not in ARTIFACT_VALIDATORS:
        raise RuntimeError(f"unknown schema '{schema_name}'")
    validator = ARTIFACT_VALIDATORS[schema_name]
    validator(payload)


def _validate_conflicts(args) -> tuple[int, Mapping[str, Any] | None]:
    if not args.flows or not args.policies:
        raise SystemExit("--flows and --policies are required to run conflict validation")
    flow_paths = _expand(args.flows)
    policy_paths = _expand(args.policies)
    flows = [_load_yaml(path) for path in flow_paths]
    policies = [_load_yaml(path) for path in policy_paths]
    conflicts = find_conflicts(flows, policies)
    if args.json:
        payload: Mapping[str, Any] = {
            "flows": [str(path) for path in flow_paths],
            "policies": [str(path) for path in policy_paths],
            "conflicts": [conflict.__dict__ for conflict in conflicts],
        }
        return (1 if conflicts else 0, payload)
    else:
        if not conflicts:
            print("no conflicts detected")
        for conflict in conflicts:
            subjects = ", ".join(conflict.subjects)
            print(f"[{conflict.kind}] {conflict.detail} :: {subjects}")
        return (1 if conflicts else 0, None)


def cmd_validate(args) -> int:
    exit_code = 0
    handled = False
    json_payload: dict[str, Any] = {}

    if args.flows or args.policies:
        handled = True
        conflict_exit, conflict_payload = _validate_conflicts(args)
        if conflict_exit:
            exit_code = 1
        if conflict_payload:
            json_payload.update(conflict_payload)

    artifact_patterns, ast_directories = _partition_artifact_arguments(args.artifacts)
    ast_documents: list[_AstDocument] = []
    artifact_files: list[Path] = []

    if artifact_patterns:
        expanded = _expand(artifact_patterns)
        ast_docs, remaining_artifacts = _collect_ast_files(expanded, force_ast=args.ast)
        ast_documents.extend(ast_docs)
        artifact_files.extend(remaining_artifacts)

    if ast_directories:
        ast_documents.extend(_collect_ast_directories(ast_directories))

    if ast_documents:
        handled = True
        seen_paths: set[Path] = set()
        deduped_docs: list[_AstDocument] = []
        for document in ast_documents:
            path = document.path
            if path in seen_paths:
                continue
            seen_paths.add(path)
            deduped_docs.append(document)
        ast_results = _validate_ast_documents(tuple(deduped_docs))
        if args.json:
            json_payload["ast"] = _ast_results_to_json(ast_results)
        else:
            _print_ast_results(ast_results)
        if any(result.issues for result in ast_results):
            exit_code = 1

    if artifact_files:
        handled = True
        if _validate_artifact_files(tuple(artifact_files), args.schema):
            exit_code = 1

    if args.json and json_payload:
        print(json.dumps(json_payload, indent=2, ensure_ascii=False))

    if not handled:
        raise SystemExit("validate: supply --flows/--policies or artifact file paths")

    return exit_code


def register_validate_command(parent) -> None:
    validate_parser = parent.add_parser(
        "validate",
        help="validate flows/policies for conflicts or artifacts for schema conformance",
    )
    validate_parser.add_argument("--flows", nargs="+", help="flow file paths/globs")
    validate_parser.add_argument("--policies", nargs="+", help="policy file paths/globs")
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON output for conflicts or canonical AST validation",
    )
    validate_parser.add_argument(
        "--schema",
        choices=sorted(ARTIFACT_VALIDATORS),
        help="explicit artifact schema to validate files against",
    )
    validate_parser.add_argument(
        "--ast",
        action="store_true",
        help="treat positional paths as canonical AST files (useful for single-file validation)",
    )
    validate_parser.add_argument(
        "artifacts",
        nargs="*",
        help="artifact files or directories (YAML/JSON) to validate",
    )
    validate_parser.set_defaults(func=cmd_validate)
