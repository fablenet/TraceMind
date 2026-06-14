"""Zero-LLM discipline check for the 5W1H completeness contract.

Phase 7 Stage 7-0 task 7-0.7. The 5W1H completeness + uncertainty-closure
modules are the *deterministic* foundation of TraceMind's formal language
(invariant 3: the K-plane is a verifier, not an LLM solver). They MUST judge
structural completeness by deterministic rules only and never import any
LLM/AI machinery.

This script AST-scans the guarded modules and fails if any of them imports an
LLM/AI namespace:

- ``tm.ai`` / ``tm.ai.*`` (provider clients, tuners, reflect/plan, …)
- ``tm.steps.ai_*`` (AI pipeline steps)
- ``tm.server.routes_llm`` (LLM HTTP routes)
- any module whose component contains ``llm`` or is a known vendor SDK
  (``openai`` / ``anthropic``)

Commented imports do not trigger (AST-based). Lazy imports inside functions are
still caught (``ast.walk`` visits every ``Import``/``ImportFrom``).

Run::

    python scripts/check_no_llm_in_completeness.py

Exits non-zero on any violation, with a description of the offense.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TM_ROOT = REPO_ROOT / "tm"

# Deterministic, zero-LLM modules that must never import LLM/AI machinery.
GUARDED_FILES: tuple[Path, ...] = (
    TM_ROOT / "intent" / "completeness.py",
    TM_ROOT / "intent" / "uncertainty.py",
    TM_ROOT / "intent" / "design_loop.py",
    TM_ROOT / "intent" / "session.py",
    TM_ROOT / "intent" / "consistency_gate.py",
    TM_ROOT / "intent" / "profile_governance.py",
    TM_ROOT / "intent" / "clarify.py",
)

_VENDOR_SDKS = {"openai", "anthropic", "cohere", "vertexai"}


def _llm_reason(module: str) -> str | None:
    """Return why ``module`` is an LLM/AI import, or None if it's allowed."""
    if not module:
        return None
    parts = module.split(".")
    if module == "tm.ai" or module.startswith("tm.ai."):
        return f"`{module}` is LLM/AI machinery (tm.ai.*)"
    if module.startswith("tm.steps.ai_") or (parts[:2] == ["tm", "steps"] and parts[-1].startswith("ai_")):
        return f"`{module}` is an AI pipeline step (tm.steps.ai_*)"
    if module.endswith("routes_llm") or "routes_llm" in parts:
        return f"`{module}` is the LLM HTTP route module"
    for component in parts:
        if "llm" in component.lower():
            return f"`{module}` has component '{component}' containing 'llm'"
    if any(component in _VENDOR_SDKS for component in parts):
        return f"`{module}` is an LLM vendor SDK"
    return None


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
                modules.append(f"{base}.{alias.name}" if base else alias.name)
        for mod in modules:
            reason = _llm_reason(mod)
            if reason is not None:
                issues.append(
                    f"{path}:{node.lineno}: forbidden LLM import {reason}; "
                    f"5W1H completeness must stay deterministic and zero-LLM "
                    f"(invariant 3)"
                )
    return issues


def main(argv: list[str] | None = None) -> int:
    issues: list[str] = []
    scanned = 0
    for path in GUARDED_FILES:
        if not path.exists():
            print(
                f"ERROR: guarded module {path} does not exist (renamed/moved?). "
                f"Update scripts/check_no_llm_in_completeness.py::GUARDED_FILES.",
                file=sys.stderr,
            )
            return 2
        scanned += 1
        issues.extend(_check_imports(path, path.read_text(encoding="utf-8")))

    if issues:
        print("Zero-LLM discipline violations detected:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print(
            f"\n{len(issues)} violation(s). The 5W1H completeness contract must "
            "remain deterministic and zero-LLM (Phase 7 Stage 7-0 task 7-0.7).",
            file=sys.stderr,
        )
        return 1

    print(f"no-LLM-in-completeness: OK ({scanned} guarded module(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
