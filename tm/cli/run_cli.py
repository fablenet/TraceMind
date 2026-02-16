from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from tm.artifacts import load_yaml_artifact
from tm.artifacts.models import AgentBundleBody, Artifact
from tm.runtime.context import ExecutionContext
from tm.runtime.executor import AgentBundleExecutor, AgentBundleExecutorError
from tm.utils.yaml import import_yaml

yaml = import_yaml()


def _parse_input_value(raw: str) -> Any:
    if raw.startswith("@"):
        path = Path(raw[1:])
        payload = path.read_text(encoding="utf-8")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_inputs(raw_inputs: Sequence[str]) -> Mapping[str, Any]:
    inputs: Dict[str, Any] = {}
    for entry in raw_inputs:
        if "=" not in entry:
            raise ValueError(f"invalid input '{entry}'; expected key=value")
        key, raw = entry.split("=", 1)
        if not key.strip():
            raise ValueError(f"input key missing in '{entry}'")
        inputs[key.strip()] = _parse_input_value(raw)
    return inputs


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _build_run_report(
    artifact: Artifact,
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    success: bool,
    error: str | None,
) -> Mapping[str, Any]:
    body = artifact.body
    metadata = context.metadata
    executed_steps = list(metadata.get("executed_steps") or [])
    step_outputs = metadata.get("step_outputs") or {}
    plan_steps = []
    if isinstance(body, AgentBundleBody):
        for step in body.plan:
            status = "completed" if step.step in executed_steps else ("pending" if success else "failed")
            plan_steps.append(
                {
                    "step": step.step,
                    "agent_id": step.agent_id,
                    "phase": step.phase,
                    "inputs": list(step.inputs),
                    "outputs": list(step.outputs),
                    "status": status,
                    "produced": step_outputs.get(step.step),
                }
            )
    evidence_records = context.evidence.records()
    evidence_summary: Dict[str, Any] = {"total": len(evidence_records), "by_kind": {}}
    for record in evidence_records:
        evidence_summary["by_kind"].setdefault(record.kind, 0)
        evidence_summary["by_kind"][record.kind] += 1
    policy_decisions = [
        {
            "effect": rec.payload.get("effect"),
            "target": rec.payload.get("target"),
            "allowed": rec.payload.get("allowed"),
            "reason": rec.payload.get("reason"),
        }
        for rec in evidence_records
        if rec.kind == "policy_guard"
    ]
    outputs: Dict[str, Any] = {}
    if isinstance(body, AgentBundleBody):
        for step in body.plan:
            for ref in step.outputs:
                try:
                    outputs[ref] = context.get_ref(ref)
                except KeyError:
                    continue
    return {
        "bundle_id": getattr(body, "bundle_id", None),
        "artifact_id": artifact.envelope.artifact_id,
        "body_hash": artifact.envelope.body_hash,
        "start_time": _format_iso(start),
        "end_time": _format_iso(end),
        "duration_seconds": (end - start).total_seconds(),
        "success": success,
        "error": error,
        "steps": plan_steps,
        "outputs": outputs,
        "evidence_summary": evidence_summary,
        "policy_decisions": policy_decisions,
    }


def _write_report(path: str | Path, report: Mapping[str, Any], *, json_output: bool) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if json_output:
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return
    if yaml is None:
        raise SystemExit("PyYAML is required to emit run reports in YAML format")
    with target.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(report, handle, sort_keys=True, allow_unicode=True)


def _cmd_run_bundle(args: argparse.Namespace) -> int:
    try:
        artifact = load_yaml_artifact(Path(args.bundle))
    except Exception as exc:
        print(f"run bundle: failed to load artifact: {exc}", file=sys.stderr)
        return 1
    executor = AgentBundleExecutor()
    context = ExecutionContext()
    try:
        inputs = _parse_inputs(args.inputs or [])
    except ValueError as exc:
        print(f"run bundle: invalid input: {exc}", file=sys.stderr)
        return 1
    for ref, value in inputs.items():
        context.set_ref(ref, value)
    start = datetime.now(timezone.utc)
    success = True
    error: str | None = None
    try:
        executor.execute(artifact, context=context)
    except AgentBundleExecutorError as exc:
        success = False
        error = str(exc)
    except Exception as exc:
        success = False
        error = str(exc)
    end = datetime.now(timezone.utc)
    report = _build_run_report(artifact, context, start, end, success, error)
    try:
        _write_report(args.report, report, json_output=bool(args.json))
    except SystemExit as exc:
        print(f"run bundle: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"run bundle: failed to write report: {exc}", file=sys.stderr)
        return 1
    status = "success" if success else "failed"
    print(f"run bundle: {status}; report -> {args.report}")
    return 0 if success else 1


def _ensure_run_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    name_map = getattr(subparsers, "_name_parser_map", {})
    run_parser = name_map.get("run")
    if run_parser is None:
        run_parser = subparsers.add_parser("run", help="execute runtime artifacts")
    return run_parser


def _ensure_run_subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    subparsers = getattr(parser, "_run_subparsers", None)
    if subparsers is None:
        subparsers = parser.add_subparsers(dest="rcmd")
        setattr(parser, "_run_subparsers", subparsers)
    return subparsers


def register_run_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = _ensure_run_parser(subparsers)
    run_sub = _ensure_run_subparsers(parser)
    bundle_map = getattr(run_sub, "_name_parser_map", {})
    if "bundle" in bundle_map:
        return
    bundle_parser = run_sub.add_parser("bundle", help="run an agent bundle artifact")
    bundle_parser.add_argument("bundle", help="path to accepted agent_bundle artifact")
    bundle_parser.add_argument(
        "--inputs",
        action="append",
        default=[],
        help="input refs formatted as key=value (value will be JSON decoded when possible)",
    )
    bundle_parser.add_argument("--report", required=True, help="path to write the run report")
    bundle_parser.add_argument("--json", action="store_true", help="write report as JSON instead of YAML")
    bundle_parser.set_defaults(func=_cmd_run_bundle)
