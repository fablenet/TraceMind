from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from tm.policy.deterministic import canonical_json_bytes
from tm.proposal.gate import run_proposal_gate
from tm.utils.yaml import import_yaml

yaml = import_yaml()
_SUFFIXES = {".json", ".yaml", ".yml"}


def _load_mapping(path: Path) -> Mapping[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            if yaml is None:
                return None
            payload = yaml.safe_load(text)
        else:
            payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    return payload


def _resolve_path(arg_value: str | None, env_key: str, default: str) -> Path:
    raw = arg_value or os.getenv(env_key) or default
    return Path(raw).expanduser()


def _cmd_ci_gate(args: argparse.Namespace) -> int:
    proposals_glob = args.proposals_glob or os.getenv("TM_CI_PROPOSALS_GLOB") or "proposals/**/*"
    intents_path = _resolve_path(args.intents, "TM_CI_INTENTS", "proposals/intents.json")
    policy_path = _resolve_path(args.policy, "TM_CI_POLICY", "proposals/policy.json")
    registry_path = _resolve_path(args.registry, "TM_CI_REGISTRY", "proposals/registry.jsonl")
    trace_path = args.trace or os.getenv("TM_CI_TRACE")

    proposal_candidates = sorted(Path(p).resolve() for p in glob.glob(proposals_glob, recursive=True))
    proposal_files: list[Path] = []
    for path in proposal_candidates:
        if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
            continue
        payload = _load_mapping(path)
        if not isinstance(payload, Mapping):
            continue
        if payload.get("kind") == "Proposal":
            proposal_files.append(path)

    if not proposal_files:
        report = {
            "summary": {"total": 0, "passed": 0, "failed": 0, "warned": 0},
            "results": [],
        }
        print(f"ci gate: no proposals found under glob '{proposals_glob}'", file=sys.stderr)
        sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
        return 0

    results: list[dict[str, Any]] = []
    failed = 0
    warned = 0
    for proposal_path in proposal_files:
        try:
            report = run_proposal_gate(
                proposal_path=proposal_path,
                intents_path=intents_path,
                policy_path=policy_path,
                registry_path=registry_path if registry_path.exists() else None,
                trace_path=Path(trace_path).expanduser() if trace_path else None,
            )
            status = report["summary"]["status"]
            proposal_warnings = int(report["summary"].get("warnings", 0))
            if status == "fail":
                failed += 1
            elif proposal_warnings > 0:
                warned += 1
            results.append(
                {
                    "proposal": str(proposal_path),
                    "status": "fail" if status == "fail" else ("warn" if proposal_warnings > 0 else "pass"),
                    "failed_step": report["summary"]["failed_step"],
                    "warnings": proposal_warnings,
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "proposal": str(proposal_path),
                    "status": "fail",
                    "failed_step": "ci_gate_exception",
                    "warnings": 0,
                    "error": str(exc),
                }
            )

    results.sort(key=lambda item: (item["proposal"], item["status"], item.get("failed_step") or ""))
    summary = {
        "total": len(results),
        "passed": sum(1 for item in results if item["status"] == "pass"),
        "failed": failed,
        "warned": warned,
    }
    out = {"summary": summary, "results": results}
    print(
        f"ci gate summary: total={summary['total']} passed={summary['passed']} failed={summary['failed']} warned={summary['warned']}",
        file=sys.stderr,
    )
    if warned > 0 and failed == 0:
        print("ci gate: warnings present but not blocking", file=sys.stderr)
    sys.stdout.buffer.write(canonical_json_bytes(out) + b"\n")
    return 1 if failed > 0 else 0


def register_ci_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("ci", help="ci utility commands")
    ci_sub = parser.add_subparsers(dest="ci_cmd")
    ci_sub.required = True

    gate = ci_sub.add_parser("gate", help="run proposal gate over discovered proposal files")
    gate.add_argument("--proposals-glob", help="glob for proposal discovery (default: proposals/**/*)")
    gate.add_argument("--intents", help="default intents path (or TM_CI_INTENTS)")
    gate.add_argument("--policy", help="default policy path (or TM_CI_POLICY)")
    gate.add_argument("--registry", help="default registry path (or TM_CI_REGISTRY)")
    gate.add_argument("--trace", help="optional trace artifact path (or TM_CI_TRACE)")
    gate.set_defaults(func=_cmd_ci_gate)


__all__ = ["register_ci_commands"]
