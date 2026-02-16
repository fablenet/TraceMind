from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tm.policy import canonical_json_bytes
from tm.policy.test_runner import run_test_suite_files


def _cmd_tests_run(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite).expanduser()
    policy_path = Path(args.policy).expanduser()
    try:
        outcome = run_test_suite_files(suite_path=suite_path, policy_path=policy_path)
    except Exception as exc:
        print(f"tests run: {exc}", file=sys.stderr)
        return 1

    report_text = canonical_json_bytes(outcome.report).decode("utf-8")
    if args.json_report:
        report_path = Path(args.json_report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")

    summary = outcome.report["summary"]
    print(
        "tests summary: "
        f"total={summary['total']} passed={summary['passed']} "
        f"failed={summary['failed']} warnings={summary['warnings']}"
    )
    for result in outcome.report["results"]:
        status = result["status"]
        if status == "warn":
            print(f"WARNING {result['test_id']}: {result['reason']}")
        elif status == "fail":
            print(f"FAIL {result['test_id']}: {result['reason']}")

    if args.json:
        print(json.dumps(outcome.report, indent=2, ensure_ascii=False))
    return outcome.exit_code


def register_tests_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tests", help="regression test and gate tools")
    tests_sub = parser.add_subparsers(dest="tcmd")

    run_parser = tests_sub.add_parser("run", help="run TestSuite against deterministic PolicyEngine")
    run_parser.add_argument("--suite", required=True, help="TestSuite AST path (.json, .yaml, .yml)")
    run_parser.add_argument("--policy", required=True, help="policy AST path (.json, .yaml, .yml)")
    run_parser.add_argument("--json-report", help="write machine-readable report JSON to file")
    run_parser.add_argument("--json", action="store_true", help="print machine-readable report JSON to stdout")
    run_parser.set_defaults(func=_cmd_tests_run)


__all__ = ["register_tests_commands"]
