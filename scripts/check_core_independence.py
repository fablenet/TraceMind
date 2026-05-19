"""Core independence discipline check.

This script enforces the "general-purpose tool" rule for ``trace-mind``:
nothing under ``tm/`` may depend on ``fablenet-*``, the FableNet backbone
gRPC ``connector`` package, or any other domain-specific scenario.

It checks two layers of leakage:

1. **Imports** — no ``import fablenet`` / ``from fablenet.* import ...`` /
   ``import connector`` / ``from connector.* import ...`` in any ``tm/``
   file (AST-based check, so commented imports do not trigger).

2. **Domain-specific magic strings** — no FableNet-anchored identifiers
   like ``"FNET-INT-"``, ``"fablenet:"`` (the actuator URI prefix used
   in FableNet's act layer), or ``"fablenet-agent/"`` (the agent ID
   prefix used in FableNet's analyze/decide/act agents) appear in
   source code outside of allow-listed paths (currently: none).

Used by ``Phase 5 Stage 5-2 task 2.8`` to keep TraceMind's core lifted
modules truly domain-neutral.

Run::

    python -m scripts.check_core_independence

Exits non-zero on any violation, with a description of the offense.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
TM_ROOT = REPO_ROOT / "tm"

FORBIDDEN_IMPORT_PREFIXES = (
    "fablenet",
    "connector",
)

FORBIDDEN_MODULE_SUFFIX_TOKENS = (
    "_pb2",
    "_pb2_grpc",
)

FORBIDDEN_STRING_PATTERNS = (
    (re.compile(r"\bFNET-INT-\d+"), "FableNet intent identifier"),
    (re.compile(r'"fablenet:[^"]+"'), "FableNet actuator URI prefix"),
    (re.compile(r'"fablenet-agent/[^"]+"'), "FableNet agent ID prefix"),
)

ALLOWED_STRING_FILES: set[Path] = set()


def _walk_py_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _check_imports(path: Path, source: str) -> list[str]:
    issues: list[str] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        issues.append(f"{path}: SyntaxError parsing for import scan: {exc}")
        return issues

    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                modules.append(base)
            for alias in node.names:
                # ``from x import y`` may import a submodule named ``y``;
                # treat each imported name as if it were a (sub)module
                # for prefix/suffix checks. False positives on plain
                # symbol imports are tolerable — domain-leakage names
                # like ``control_pb2`` or ``fablenet_xyz`` should not be
                # exported names from a domain-neutral module anyway.
                qualified = f"{base}.{alias.name}" if base else alias.name
                modules.append(qualified)
        for mod in modules:
            top = mod.split(".")[0]
            if top in FORBIDDEN_IMPORT_PREFIXES:
                issues.append(
                    f"{path}:{node.lineno}: forbidden import "
                    f"`{mod}` (top-level package `{top}` is domain-specific; "
                    f"tm/ must remain domain-neutral)"
                )
            tail = mod.split(".")[-1]
            for token in FORBIDDEN_MODULE_SUFFIX_TOKENS:
                if mod.endswith(token) or tail.endswith(token):
                    issues.append(
                        f"{path}:{node.lineno}: forbidden import "
                        f"`{mod}` (looks like generated protobuf — tm/ "
                        f"must not depend on any domain's proto bindings)"
                    )
                    break
    return issues


def _check_magic_strings(path: Path, source: str) -> list[str]:
    if path in ALLOWED_STRING_FILES:
        return []
    issues: list[str] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, label in FORBIDDEN_STRING_PATTERNS:
            for match in pattern.finditer(line):
                issues.append(
                    f"{path}:{lineno}: forbidden domain-specific literal "
                    f"`{match.group(0)}` ({label}); use a parameter or "
                    f"a neutral placeholder"
                )
    return issues


def main(argv: list[str] | None = None) -> int:
    if not TM_ROOT.exists():
        print(f"ERROR: {TM_ROOT} does not exist", file=sys.stderr)
        return 2

    issues: list[str] = []
    for py in _walk_py_files(TM_ROOT):
        source = py.read_text(encoding="utf-8")
        issues.extend(_check_imports(py, source))
        issues.extend(_check_magic_strings(py, source))

    if issues:
        print("Core independence violations detected:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print(
            f"\n{len(issues)} violation(s). "
            "trace-mind core must remain domain-neutral (Phase 5 Stage 5-2 "
            "task 2.8). See TraceMind/scripts/check_core_independence.py "
            "for the policy.",
            file=sys.stderr,
        )
        return 1

    print(f"core independence: OK ({sum(1 for _ in _walk_py_files(TM_ROOT))} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
