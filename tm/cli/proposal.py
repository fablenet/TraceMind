from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from tm.policy.deterministic import canonical_json_bytes
from tm.proposal import validate_proposal
from tm.utils.yaml import import_yaml

yaml = import_yaml()


def _load_mapping(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML is required to read YAML files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return payload


def _cmd_proposal_validate(args: argparse.Namespace) -> int:
    proposal_path = Path(args.proposal).expanduser()
    intents_path = Path(args.intents).expanduser()
    policy_path = Path(args.policy).expanduser() if args.policy else None
    try:
        proposal = _load_mapping(proposal_path)
        intents = _load_mapping(intents_path)
        policy = _load_mapping(policy_path) if policy_path else None
    except Exception as exc:
        print(f"proposal validate: failed to load input: {exc}", file=sys.stderr)
        return 1

    report = validate_proposal(
        proposal=proposal,
        intent_tree=intents,
        policy=policy,
        base_paths=[proposal_path.parent, Path.cwd()],
    )
    summary = report["summary"]
    reasons: dict[str, int] = {}
    for row in report["errors"]:
        code = row["code"]
        reasons[code] = reasons.get(code, 0) + 1
    print(
        "proposal validate summary: "
        f"errors={summary['errors']} warnings={summary['warnings']} reasons={dict(sorted(reasons.items()))}",
        file=sys.stderr,
    )

    if args.json:
        sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    else:
        if summary["errors"] == 0 and summary["warnings"] == 0:
            print("proposal: valid")
        else:
            for row in report["errors"]:
                print(f"ERROR {row['code']} {row['path']} {row['message']}")
            for row in report["warnings"]:
                print(f"WARN {row['code']} {row['path']} {row['message']}")
    return 0 if summary["errors"] == 0 else 1


def register_proposal_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("proposal", help="proposal lint validation tools")
    proposal_sub = parser.add_subparsers(dest="proposal_cmd")
    proposal_sub.required = True

    validate_parser = proposal_sub.add_parser("validate", help="lint validate Proposal against intents/tests refs")
    validate_parser.add_argument("--proposal", required=True, help="Proposal AST path (.json, .yaml, .yml)")
    validate_parser.add_argument("--intents", required=True, help="IntentTree AST path (.json, .yaml, .yml)")
    validate_parser.add_argument("--policy", help="optional Policy AST path (.json, .yaml, .yml)")
    validate_parser.add_argument("--json", action="store_true", help="emit canonical machine-readable report")
    validate_parser.set_defaults(func=_cmd_proposal_validate)


__all__ = ["register_proposal_commands"]
