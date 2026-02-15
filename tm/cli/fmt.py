from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.ast.canonical import canonical_dumps
from tm.utils.yaml import import_yaml

yaml = import_yaml()
_SUPPORTED_SUFFIXES = {".json", ".yaml", ".yml"}


@dataclass(frozen=True)
class _FmtResult:
    path: Path
    changed: bool
    error: str | None = None


def _find_supported_files(target: Path) -> tuple[Path, ...]:
    resolved = target.resolve()
    if not resolved.exists():
        raise ValueError(f"{target}: no such file or directory")
    if resolved.is_file():
        if resolved.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise ValueError(f"{resolved}: unsupported file type (expected .json/.yaml/.yml)")
        return (resolved,)
    files = tuple(
        sorted(path for path in resolved.rglob("*") if path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES)
    )
    if not files:
        raise ValueError(f"{resolved}: no .json/.yaml/.yml files found")
    return files


def _load_mapping(path: Path) -> Mapping[str, Any]:
    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML required to read YAML files; install with `pip install pyyaml`")
        payload = yaml.safe_load(raw) or {}
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("expected top-level object")
    return payload


def _validate_canonical_ast(payload: Mapping[str, Any]) -> None:
    from tm.ast.validator import validate_canonical_ast

    issues = validate_canonical_ast(payload)
    if not issues:
        return
    joined = "; ".join(f"{issue.json_path}: {issue.message}" for issue in issues)
    raise ValueError(f"canonical AST validation failed: {joined}")


def _format_one(path: Path, *, write: bool, validate: bool) -> _FmtResult:
    original = path.read_text(encoding="utf-8")
    payload = _load_mapping(path)
    canonical = canonical_dumps(payload)
    if validate:
        canonical_payload = json.loads(canonical)
        _validate_canonical_ast(canonical_payload)
    changed = original != canonical
    if changed and write:
        path.write_text(canonical, encoding="utf-8")
    return _FmtResult(path=path, changed=changed)


def _results_to_json(results: Sequence[_FmtResult]) -> str:
    payload = {
        "files": [
            {
                "file": str(result.path),
                "changed": result.changed,
                "error": result.error,
            }
            for result in results
        ]
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _print_human_results(results: Sequence[_FmtResult], *, check: bool) -> None:
    for result in results:
        if result.error:
            print(f"{result.path}: {result.error}", file=sys.stderr)
            continue
        if check:
            if result.changed:
                print(f"{result.path}: non-canonical")
            else:
                print(f"{result.path}: canonical")
            continue
        print(f"{result.path}: {'formatted' if result.changed else 'already canonical'}")


def _cmd_fmt(args: argparse.Namespace) -> int:
    try:
        files = _find_supported_files(Path(args.target))
    except ValueError as exc:
        print(f"fmt: {exc}", file=sys.stderr)
        return 1

    write = not args.check
    results: list[_FmtResult] = []
    for path in files:
        try:
            results.append(_format_one(path, write=write, validate=not args.no_validate))
        except Exception as exc:
            results.append(_FmtResult(path=path, changed=False, error=str(exc)))

    if args.json:
        print(_results_to_json(results))
    else:
        _print_human_results(results, check=args.check)

    has_errors = any(result.error for result in results)
    has_changes = any(result.changed for result in results)
    if has_errors:
        return 1
    if args.check and has_changes:
        return 1
    return 0


def register_fmt_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "fmt",
        help="canonical AST formatter for JSON/YAML files",
        description=(
            "Format canonical AST documents into stable canonical JSON. "
            "Default mode writes files in place; --check reports non-canonical files without writing."
        ),
    )
    parser.add_argument("target", help="file or directory to format")
    parser.add_argument("--check", action="store_true", help="check only; do not write files")
    parser.add_argument("--write", action="store_true", help="write files in place (default unless --check)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip canonical AST validation before reporting/writing",
    )
    parser.set_defaults(func=_cmd_fmt)
