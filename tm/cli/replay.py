from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tm.policy.deterministic import canonical_json_bytes
from tm.policy.replay import diff_replay_files, diff_replay_rows, replay_trace, replay_trace_rows


def _cmd_replay_run(args: argparse.Namespace) -> int:
    trace_path = Path(args.trace).expanduser()
    policy_path = Path(args.policy).expanduser()
    out_path = Path(args.out).expanduser()
    try:
        total = replay_trace(trace_path=trace_path, policy_path=policy_path, out_path=out_path)
    except Exception as exc:
        print(f"replay run: {exc}", file=sys.stderr)
        return 1
    print(f"replay completed: events={total} out={out_path}")
    return 0


def _cmd_replay_diff(args: argparse.Namespace) -> int:
    try:
        if args.old and args.new:
            report = diff_replay_files(old_path=Path(args.old).expanduser(), new_path=Path(args.new).expanduser())
        else:
            if not (args.trace and args.policy_old and args.policy_new):
                print(
                    "replay diff: either provide --old/--new or provide --trace/--policy-old/--policy-new",
                    file=sys.stderr,
                )
                return 1
            old_rows = replay_trace_rows(
                trace_path=Path(args.trace).expanduser(),
                policy_path=Path(args.policy_old).expanduser(),
            )
            new_rows = replay_trace_rows(
                trace_path=Path(args.trace).expanduser(),
                policy_path=Path(args.policy_new).expanduser(),
            )
            report = diff_replay_rows(old_rows=old_rows, new_rows=new_rows)
    except Exception as exc:
        print(f"replay diff: {exc}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        "replay diff summary: "
        f"changed_rows={summary['changed_rows']} "
        f"added_rows={summary['added_rows']} removed_rows={summary['removed_rows']}"
    )
    for item in report["by_rule"]:
        print(
            f"  rule {item['rule_id']}: "
            f"changed_rows={item['changed_rows']} "
            f"add={item['action_added']} rm={item['action_removed']} mod={item['action_modified']}"
        )

    if args.json_report:
        report_path = Path(args.json_report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(canonical_json_bytes(report).decode("utf-8"), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def register_replay_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("replay", help="deterministic replay tools")
    replay_sub = parser.add_subparsers(dest="rcmd")

    run_parser = replay_sub.add_parser("run", help="replay trace.jsonl through PolicyEngine")
    run_parser.add_argument("--trace", required=True, help="input trace JSONL path")
    run_parser.add_argument("--policy", required=True, help="policy JSON/YAML path")
    run_parser.add_argument("--out", required=True, help="output replay JSONL path")
    run_parser.set_defaults(func=_cmd_replay_run)

    diff_parser = replay_sub.add_parser("diff", help="diff two replay outputs or two policies on one trace")
    diff_parser.add_argument("--old", help="old replay JSONL path")
    diff_parser.add_argument("--new", help="new replay JSONL path")
    diff_parser.add_argument("--trace", help="input trace JSONL path")
    diff_parser.add_argument("--policy-old", help="old policy JSON/YAML path")
    diff_parser.add_argument("--policy-new", help="new policy JSON/YAML path")
    diff_parser.add_argument("--json-report", help="write machine-readable diff report JSON")
    diff_parser.add_argument("--json", action="store_true", help="print machine-readable diff report JSON")
    diff_parser.set_defaults(func=_cmd_replay_diff)


__all__ = ["register_replay_commands"]
