from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from tm.intent.coverage import compute_intents_coverage
from tm.intent.tree_validator import validate_intent_tree
from tm.policy.deterministic import canonical_json_bytes
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


def _cmd_intents_validate(args: argparse.Namespace) -> int:
    file_path = Path(args.intents).expanduser()
    try:
        payload = _load_mapping(file_path)
    except Exception as exc:
        print(f"intents validate: failed to load {file_path}: {exc}", file=sys.stderr)
        return 1

    issues = validate_intent_tree(payload)
    intents_count = 0
    if isinstance(payload.get("intents"), list):
        intents_count = len(payload["intents"])
    elif isinstance(payload.get("spec"), Mapping):
        spec = payload["spec"]
        if isinstance(spec.get("intents"), list):
            intents_count = len(spec["intents"])
        elif isinstance(spec.get("spec"), Mapping) and isinstance(spec["spec"].get("intents"), list):
            intents_count = len(spec["spec"]["intents"])
    rows = sorted(
        [
            {
                "intent_id": issue.intent_id,
                "path": issue.path,
                "message": issue.message,
            }
            for issue in issues
        ],
        key=lambda item: ((item["intent_id"] or ""), item["path"], item["message"]),
    )
    reason_counts: dict[str, int] = {
        "duplicate_id": 0,
        "parent": 0,
        "cycle": 0,
        "related": 0,
        "leaf_success_criteria": 0,
        "other": 0,
    }
    for row in rows:
        message = row["message"]
        if "duplicate id" in message:
            reason_counts["duplicate_id"] += 1
        elif "cycle detected" in message:
            reason_counts["cycle"] += 1
        elif "parent_intent" in message or "roots" in message or "root intent" in message:
            reason_counts["parent"] += 1
        elif "related" in message:
            reason_counts["related"] += 1
        elif "success_criteria" in message:
            reason_counts["leaf_success_criteria"] += 1
        else:
            reason_counts["other"] += 1

    summary = {
        "file": str(file_path),
        "intents": intents_count,
        "errors": len(rows),
        "reasons": {k: v for k, v in reason_counts.items() if v > 0},
    }
    report = {"summary": summary, "errors": rows}
    print(
        f"validate summary: file={file_path} intents={intents_count} errors={len(rows)} "
        f"reasons={summary['reasons'] or {'none': 0}}",
        file=sys.stderr,
    )
    if args.json:
        sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    else:
        if not rows:
            print(f"{file_path}: valid")
        else:
            print(f"{file_path}: invalid ({len(rows)} errors)")
            for row in rows:
                iid = row["intent_id"] if row["intent_id"] is not None else "-"
                print(f"  intent={iid} path={row['path']} message={row['message']}")
    return 0 if not rows else 1


def _cmd_intents_coverage(args: argparse.Namespace) -> int:
    intents_path = Path(args.intents).expanduser()
    tests_path = Path(args.tests).expanduser()
    policy_path = Path(args.policy).expanduser() if args.policy else None
    try:
        outcome = compute_intents_coverage(
            intents_path=intents_path,
            tests_path=tests_path,
            policy_path=policy_path,
        )
    except Exception as exc:
        print(f"intents coverage: {exc}", file=sys.stderr)
        return 1

    summary = outcome.report["summary"]
    print(
        "coverage summary: "
        f"intents={summary['total_intents']} leaf={summary['leaf_intents']} "
        f"tests={summary['tests_scanned']} uncovered_leaf={summary['uncovered_leaf_intents']} "
        f"warnings={summary['warnings']}",
        file=sys.stderr,
    )
    payload = canonical_json_bytes(outcome.report)
    sys.stdout.buffer.write(payload + b"\n")
    return outcome.exit_code


def _cmd_intents_check_5w1h(args: argparse.Namespace) -> int:
    from tm.intent.completeness import compute_5w1h_completeness

    intent_path = Path(args.intents).expanduser()
    plan_path = Path(args.plan).expanduser() if args.plan else None
    network_path = Path(args.network).expanduser() if args.network else None
    patterns_dir = Path(args.patterns).expanduser() if args.patterns else None
    try:
        dispositions = None
        if args.dispositions:
            from tm.intent.uncertainty import load_dispositions

            dispositions = load_dispositions(Path(args.dispositions).expanduser())
        outcome = compute_5w1h_completeness(
            intent_path=intent_path,
            profile=args.profile,
            plan_path=plan_path,
            network_path=network_path,
            mode=args.mode,
            dispositions=dispositions,
            patterns_dir=patterns_dir,
        )
    except Exception as exc:
        print(f"intents check-5w1h: {exc}", file=sys.stderr)
        return 1

    summary = outcome.report["summary"]
    closure = ""
    if outcome.report["mode"] == "seal":
        closure = (
            f" sealed={outcome.report.get('sealed')} "
            f"closed_by_disposition={summary.get('closed_by_disposition', 0)}"
        )
    print(
        "check-5w1h summary: "
        f"profile={outcome.report['profile']} mode={outcome.report['mode']} "
        f"intent={outcome.report['intent_id'] or '-'} "
        f"satisfied={summary['satisfied']}/{summary['total']} "
        f"errors={summary['errors']} warnings={summary['warnings']} "
        f"missing={outcome.report['missing_dimensions'] or '[]'}{closure}",
        file=sys.stderr,
    )
    sys.stdout.buffer.write(canonical_json_bytes(outcome.report) + b"\n")
    return outcome.exit_code


def register_intents_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("intents", help="intent tree topology validation")
    intents_sub = parser.add_subparsers(dest="intents_cmd")
    intents_sub.required = True

    validate_parser = intents_sub.add_parser(
        "validate",
        help="validate intent tree ids/topology/leaf requirements",
    )
    validate_parser.add_argument("--intents", required=True, help="intent tree AST path (.json, .yaml, .yml)")
    validate_parser.add_argument("--json", action="store_true", help="emit canonical machine-readable validation")
    validate_parser.set_defaults(func=_cmd_intents_validate)

    coverage_parser = intents_sub.add_parser(
        "coverage",
        help="compute intent coverage from TestSuite intent_refs",
    )
    coverage_parser.add_argument("--intents", required=True, help="intent tree AST path (.json, .yaml, .yml)")
    coverage_parser.add_argument(
        "--tests",
        required=True,
        help="TestSuite path or directory (.json, .yaml, .yml)",
    )
    coverage_parser.add_argument(
        "--policy",
        help="optional policy AST path (.json, .yaml, .yml) to include rule intent_refs stats",
    )
    coverage_parser.set_defaults(func=_cmd_intents_coverage)

    check_parser = intents_sub.add_parser(
        "check-5w1h",
        help="check 5W1H structural completeness of a single intent (deterministic)",
    )
    check_parser.add_argument("--intents", required=True, help="intent body/artifact path (.json, .yaml, .yml)")
    check_parser.add_argument("--profile", default="base", help="5W1H profile name or path (default: base)")
    check_parser.add_argument(
        "--mode",
        choices=["design", "seal"],
        default="design",
        help="design=heuristic-tolerant exploration (default); seal=strict gate before sign-off",
    )
    check_parser.add_argument("--plan", help="optional Plan artifact path (When/How evidence)")
    check_parser.add_argument("--network", help="optional AgentNetwork artifact path (Where evidence)")
    check_parser.add_argument(
        "--patterns",
        help="optional PropertyPattern library dir (When liveness evidence; default: shipped seeds)",
    )
    check_parser.add_argument(
        "--dispositions",
        help="optional dispositions file (dim -> resolved/waived/dynamic), consumed only in --mode seal",
    )
    check_parser.set_defaults(func=_cmd_intents_check_5w1h)


__all__ = ["register_intents_commands"]
