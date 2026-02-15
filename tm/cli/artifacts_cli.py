from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from tm.artifacts import (
    ArtifactRegistry,
    ArtifactVerificationReport,
    ConsistencyReport,
    DiffReport,
    check_consistency,
    diff_artifacts,
    default_registry,
    load_yaml_artifact,
    verify,
)
from tm.artifacts.registry import RegistryStorage
from tm.lint.plan_lint import LintIssue, lint_plan
from tm.utils.yaml import import_yaml

yaml = import_yaml()


def _load_plan(path: Path) -> Mapping[str, Any]:
    if yaml is None:
        raise SystemExit("PyYAML is required for plan linting; install the 'yaml' extra.")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SystemExit(f"failed to load plan: {exc}")
    if not isinstance(payload, Mapping):
        raise SystemExit(f"{path}: expected mapping document")
    return payload


def _dump_yaml(document: Mapping[str, Any], path: Path) -> None:
    if yaml is None:
        raise SystemExit("PyYAML is required to emit artifacts; install the 'yaml' extra.")
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=True, allow_unicode=True)


def _envelope_to_mapping(envelope: Any) -> Mapping[str, Any]:
    data = asdict(envelope)
    data["status"] = envelope.status.value
    data["artifact_type"] = envelope.artifact_type.value
    if envelope.signature is None:
        data.pop("signature", None)
    return data


def _print_verification_report(report: ArtifactVerificationReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return
    status = "accepted" if report.success else "rejected"
    print(f"{report.artifact_id}: {status}")
    if report.errors:
        for error in report.errors:
            print(f"  - {error}")


def _print_diff_report(report: DiffReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report.machine_readable(), indent=2, ensure_ascii=False))
        return
    print(report.human_summary())
    for line in report.diff_lines:
        print(line)


def _print_lint_issues(issues: list[LintIssue], *, json_output: bool) -> None:
    if json_output:
        payload = [
            {"code": issue.code, "message": issue.message, "severity": issue.severity, "path": issue.path}
            for issue in issues
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not issues:
        print("plan lint: no issues")
        return
    for issue in issues:
        path = f" ({issue.path})" if issue.path else ""
        print(f"[{issue.severity}] {issue.code}: {issue.message}{path}")


def _load_registry(path: str | None) -> ArtifactRegistry:
    if path:
        return ArtifactRegistry(storage=RegistryStorage(Path(path).expanduser()))
    return default_registry()


def _print_consistency_report(report: ConsistencyReport, *, json_output: bool) -> None:
    if json_output:
        payload = {"artifact_id": report.artifact_id, "issues": report.machine_readable}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"{report.artifact_id}: {report.human_summary}")
    for issue in report.issues:
        print(f"  - [{issue.severity}] {issue.code}: {issue.summary}")


def _consistency_has_errors(report: ConsistencyReport) -> bool:
    return any(issue.severity.lower() == "error" for issue in report.issues)


def _cmd_artifacts_verify(args: argparse.Namespace) -> int:
    try:
        artifact = load_yaml_artifact(Path(args.path))
    except Exception as exc:
        print(f"artifacts verify: failed to load artifact: {exc}", file=sys.stderr)
        return 1

    accepted, report = verify(artifact)
    _print_verification_report(report, json_output=args.json)
    return 0 if accepted and report.success else 1


def _cmd_artifacts_accept(args: argparse.Namespace) -> int:
    try:
        artifact = load_yaml_artifact(Path(args.path))
    except Exception as exc:
        print(f"artifacts accept: failed to load artifact: {exc}", file=sys.stderr)
        return 1

    accepted, report = verify(artifact)
    if not accepted or not report.success:
        _print_verification_report(report, json_output=args.json)
        return 1

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{accepted.envelope.artifact_id}.yaml"
    document = {
        "envelope": dict(_envelope_to_mapping(accepted.envelope)),
        "body": dict(accepted.body_raw),
    }
    try:
        _dump_yaml(document, output_path)
    except Exception as exc:
        print(f"artifacts accept: failed to write output: {exc}", file=sys.stderr)
        return 1

    registry = _load_registry(args.registry)

    if not args.no_consistency_gate:
        consistency_report = check_consistency(accepted, registry)
        has_errors = _consistency_has_errors(consistency_report)
        if consistency_report.issues:
            _print_consistency_report(consistency_report, json_output=args.json)
        if has_errors:
            _print_verification_report(report, json_output=args.json)
            return 1

    registry.add(accepted, output_path)
    _print_verification_report(report, json_output=args.json)
    print(f"accepted artifact written to {output_path}")
    return 0


def _cmd_artifacts_diff(args: argparse.Namespace) -> int:
    try:
        current = load_yaml_artifact(Path(args.current))
        previous = load_yaml_artifact(Path(args.previous))
    except Exception as exc:
        print(f"artifacts diff: failed to load artifacts: {exc}", file=sys.stderr)
        return 1
    report = diff_artifacts(current=current, previous=previous)
    _print_diff_report(report, json_output=args.json)
    return 0


def _cmd_plan_lint(args: argparse.Namespace) -> int:
    plan = _load_plan(Path(args.plan_path))
    issues = lint_plan(plan)
    _print_lint_issues(issues, json_output=args.json)
    return 1 if issues else 0


def _cmd_artifacts_consistency(args: argparse.Namespace) -> int:
    try:
        artifact = load_yaml_artifact(Path(args.artifact))
    except Exception as exc:
        print(f"artifacts consistency: failed to load artifact: {exc}", file=sys.stderr)
        return 1
    registry = _load_registry(args.registry)
    report = check_consistency(artifact, registry)
    _print_consistency_report(report, json_output=args.json)
    return 1 if _consistency_has_errors(report) else 0


def register_artifacts_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("artifacts", help="artifact lifecycle tools")
    artifact_sub = parser.add_subparsers(dest="acmd")

    verify_parser = artifact_sub.add_parser("verify", help="verify a candidate artifact")
    verify_parser.add_argument("path", help="artifact YAML/JSON file")
    verify_parser.add_argument("--json", action="store_true", help="emit JSON output")
    verify_parser.set_defaults(func=_cmd_artifacts_verify)

    accept_parser = artifact_sub.add_parser("accept", help="verify and persist an accepted artifact")
    accept_parser.add_argument("path", help="candidate artifact YAML/JSON file")
    accept_parser.add_argument("--out", required=True, help="output directory for accepted artifact")
    accept_parser.add_argument("--registry", help="path to registry JSONL file")
    accept_parser.add_argument("--json", action="store_true", help="emit JSON output")
    accept_parser.add_argument(
        "--no-consistency-gate", action="store_true", help="skip consistency checks before writing registry"
    )
    accept_parser.set_defaults(func=_cmd_artifacts_accept)

    diff_parser = artifact_sub.add_parser("diff", help="diff two artifacts")
    diff_parser.add_argument("current", help="current artifact path")
    diff_parser.add_argument("previous", help="previous artifact path")
    diff_parser.add_argument("--json", action="store_true", help="emit JSON output")
    diff_parser.set_defaults(func=_cmd_artifacts_diff)

    consistency_parser = artifact_sub.add_parser("consistency", help="check registry consistency for an artifact")
    consistency_parser.add_argument("--artifact", required=True, help="artifact YAML/JSON file to analyze")
    consistency_parser.add_argument("--registry", help="path to registry JSONL file")
    consistency_parser.add_argument("--json", action="store_true", help="emit JSON output")
    consistency_parser.set_defaults(func=_cmd_artifacts_consistency)


def register_plan_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("plan", help="plan utilities")
    plan_sub = parser.add_subparsers(dest="pcmd")

    lint_parser = plan_sub.add_parser("lint", help="lint a plan descriptor")
    lint_parser.add_argument("plan_path", help="path to plan YAML")
    lint_parser.add_argument("--json", action="store_true", help="emit JSON output")
    lint_parser.set_defaults(func=_cmd_plan_lint)
