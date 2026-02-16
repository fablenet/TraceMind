from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tm.policy.deterministic import canonical_json_bytes
from tm.proposal.gate import run_proposal_gate


def _cmd_gate_proposal(args: argparse.Namespace) -> int:
    try:
        report = run_proposal_gate(
            proposal_path=Path(args.proposal).expanduser(),
            intents_path=Path(args.intents).expanduser(),
            policy_path=Path(args.policy).expanduser(),
            registry_path=Path(args.registry).expanduser() if args.registry else None,
            trace_path=Path(args.trace).expanduser() if args.trace else None,
        )
    except Exception as exc:
        print(f"gate proposal: {exc}", file=sys.stderr)
        return 1

    for step in report["steps"]:
        status = step["status"]
        print(
            f"[{status}] {step['name']} errors={len(step['errors'])} warnings={len(step['warnings'])}",
            file=sys.stderr,
        )

    payload = canonical_json_bytes(report) + b"\n"
    sys.stdout.buffer.write(payload)
    if args.json_report:
        out = Path(args.json_report).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
    return 0 if report["summary"]["status"] == "pass" else 1


def register_gate_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("gate", help="gate orchestration commands")
    gate_sub = parser.add_subparsers(dest="gate_cmd")
    gate_sub.required = True

    proposal = gate_sub.add_parser("proposal", help="run proposal end-to-end semantic gate")
    proposal.add_argument("--proposal", required=True, help="Proposal AST path")
    proposal.add_argument("--intents", required=True, help="IntentTree AST path")
    proposal.add_argument("--policy", required=True, help="policy path for tests and consistency fallback")
    proposal.add_argument("--registry", help="registry JSONL path")
    proposal.add_argument("--json-report", help="write canonical JSON report to this path")
    proposal.add_argument("--trace", help="optional artifact path for consistency gate input")
    proposal.set_defaults(func=_cmd_gate_proposal)


__all__ = ["register_gate_commands"]
