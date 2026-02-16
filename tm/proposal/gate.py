from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.artifacts import ArtifactRegistry, check_consistency, load_yaml_artifact
from tm.artifacts.registry import RegistryStorage
from tm.ast import SUPPORTED_CANONICAL_KINDS, validate_canonical_ast
from tm.intent.tree_validator import validate_intent_tree
from tm.policy.test_runner import run_test_suite_files
from tm.proposal.validate import validate_proposal
from tm.utils.yaml import import_yaml

yaml = import_yaml()


def _load_mapping(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML is required to read YAML files")
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: expected mapping document")
    return payload


def _collect_refs(spec: Mapping[str, Any], *, list_key: str, obj_key: str) -> list[str]:
    refs: list[str] = []
    raw_list = spec.get(list_key)
    if isinstance(raw_list, Sequence) and not isinstance(raw_list, (str, bytes, bytearray)):
        refs.extend([item for item in raw_list if isinstance(item, str) and item])
    raw_objs = spec.get(obj_key)
    if isinstance(raw_objs, Sequence) and not isinstance(raw_objs, (str, bytes, bytearray)):
        for item in raw_objs:
            if not isinstance(item, Mapping):
                continue
            ref = item.get("ref")
            if isinstance(ref, str) and ref:
                refs.append(ref)
    return refs


def _resolve_refs(base_dir: Path, refs: Sequence[str]) -> list[Path]:
    return [((base_dir / ref).expanduser().resolve()) for ref in refs]


def _step_result(
    name: str, status: str, *, errors: Sequence[Mapping[str, Any]] = (), warnings: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "errors": list(errors),
        "warnings": list(warnings),
    }


def run_proposal_gate(
    *,
    proposal_path: Path,
    intents_path: Path,
    policy_path: Path,
    registry_path: Path | None = None,
    trace_path: Path | None = None,
) -> Mapping[str, Any]:
    steps: list[dict[str, Any]] = []
    proposal = _load_mapping(proposal_path)
    proposal_spec = proposal.get("spec")
    if isinstance(proposal_spec, Mapping):
        patch_refs = _collect_refs(proposal_spec, list_key="patch_refs", obj_key="patches")
        testsuite_refs = _collect_refs(proposal_spec, list_key="testsuite_refs", obj_key="tests")
    else:
        patch_refs = []
        testsuite_refs = []
    patch_paths = _resolve_refs(proposal_path.parent, patch_refs)
    testsuite_paths = _resolve_refs(proposal_path.parent, testsuite_refs)

    # Step 1: schema validate proposal / patches / testsuites.
    schema_errors: list[dict[str, Any]] = []
    schema_targets = [proposal_path, *patch_paths, *testsuite_paths]
    for path in schema_targets:
        try:
            payload = _load_mapping(path)
        except Exception as exc:
            schema_errors.append({"path": str(path), "message": f"load failed: {exc}"})
            continue
        kind = payload.get("kind")
        if path == proposal_path:
            if kind != "Proposal":
                schema_errors.append({"path": str(path), "message": "proposal kind must be 'Proposal'"})
            continue
        if not isinstance(kind, str) or kind not in SUPPORTED_CANONICAL_KINDS:
            schema_errors.append({"path": str(path), "message": "unsupported or missing canonical AST kind"})
            continue
        issues = validate_canonical_ast(payload)
        for issue in issues:
            schema_errors.append({"path": f"{path}::{issue.json_path}", "message": issue.message})
    if schema_errors:
        steps.append(
            _step_result(
                "schema_validate", "fail", errors=sorted(schema_errors, key=lambda x: (x["path"], x["message"]))
            )
        )
        return _finalize_gate(steps)
    steps.append(_step_result("schema_validate", "pass"))

    # Step 2: intents validate.
    intents_payload = _load_mapping(intents_path)
    intent_issues = validate_intent_tree(intents_payload)
    if intent_issues:
        errors = [
            {"path": issue.path, "message": issue.message, "intent_id": issue.intent_id}
            for issue in sorted(intent_issues, key=lambda i: (i.intent_id or "", i.path, i.message))
        ]
        steps.append(_step_result("intents_validate", "fail", errors=errors))
        return _finalize_gate(steps)
    steps.append(_step_result("intents_validate", "pass"))

    # Step 3: proposal lint validate.
    lint_report = validate_proposal(
        proposal=proposal,
        intent_tree=intents_payload,
        policy=_load_mapping(policy_path),
        base_paths=[proposal_path.parent, Path.cwd()],
    )
    lint_errors = lint_report["errors"]
    lint_warnings = [f"{row['code']}: {row['message']}" for row in lint_report["warnings"]]
    if lint_errors:
        steps.append(_step_result("proposal_lint_validate", "fail", errors=lint_errors, warnings=lint_warnings))
        return _finalize_gate(steps)
    steps.append(_step_result("proposal_lint_validate", "pass", warnings=lint_warnings))

    # Step 4: run tests.
    test_errors: list[dict[str, Any]] = []
    test_warnings: list[str] = []
    for suite_path in testsuite_paths:
        outcome = run_test_suite_files(suite_path=suite_path, policy_path=policy_path)
        for result in outcome.report["results"]:
            if result["status"] == "fail":
                test_errors.append(
                    {
                        "path": str(suite_path),
                        "message": f"{result['test_id']}: {result['reason']}",
                    }
                )
            elif result["status"] == "warn":
                test_warnings.append(f"{suite_path}:{result['test_id']}: {result['reason']}")
    if test_errors:
        steps.append(
            _step_result(
                "run_tests",
                "fail",
                errors=sorted(test_errors, key=lambda x: (x["path"], x["message"])),
                warnings=sorted(test_warnings),
            )
        )
        return _finalize_gate(steps)
    steps.append(_step_result("run_tests", "pass", warnings=sorted(test_warnings)))

    # Step 5: consistency gate.
    if registry_path is None or not registry_path.exists():
        steps.append(_step_result("consistency_gate", "pass", warnings=["registry missing; consistency gate skipped"]))
        return _finalize_gate(steps)

    artifact_path = (trace_path or policy_path).expanduser()
    try:
        artifact = load_yaml_artifact(artifact_path)
    except Exception as exc:
        steps.append(
            _step_result("consistency_gate", "fail", errors=[{"path": str(artifact_path), "message": str(exc)}])
        )
        return _finalize_gate(steps)
    registry = ArtifactRegistry(storage=RegistryStorage(registry_path.expanduser()))
    consistency = check_consistency(artifact, registry)
    c3_errors = []
    other_warnings: list[str] = []
    for issue in consistency.issues:
        if issue.code == "C3" and issue.severity.lower() == "error":
            c3_errors.append({"path": str(artifact_path), "message": issue.summary})
        else:
            other_warnings.append(f"{issue.code}:{issue.summary}")
    if c3_errors:
        steps.append(
            _step_result(
                "consistency_gate",
                "fail",
                errors=sorted(c3_errors, key=lambda x: x["message"]),
                warnings=sorted(other_warnings),
            )
        )
        return _finalize_gate(steps)
    steps.append(_step_result("consistency_gate", "pass", warnings=sorted(other_warnings)))
    return _finalize_gate(steps)


def _finalize_gate(steps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    status = "pass"
    failed_step = None
    warnings = 0
    for step in steps:
        warnings += len(step.get("warnings", []))
        if step["status"] == "fail":
            status = "fail"
            failed_step = step["name"]
            break
    report = {
        "summary": {
            "status": status,
            "failed_step": failed_step,
            "steps_total": len(steps),
            "warnings": warnings,
        },
        "steps": list(steps),
    }
    return report


__all__ = ["run_proposal_gate"]
