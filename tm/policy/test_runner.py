from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.policy.deterministic import PolicyEngine, canonical_json_bytes
from tm.utils.yaml import import_yaml

yaml = import_yaml()


@dataclass(frozen=True)
class PolicyRunOutcome:
    exit_code: int
    report: Mapping[str, Any]


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


def _match_contains(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        for key, exp_value in expected.items():
            if key not in actual:
                return False
            if not _match_contains(exp_value, actual[key]):
                return False
        return True
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)):
        if not isinstance(actual, Sequence) or isinstance(actual, (str, bytes, bytearray)):
            return False
        idx = 0
        for exp_item in expected:
            matched = False
            while idx < len(actual):
                if _match_contains(exp_item, actual[idx]):
                    matched = True
                    idx += 1
                    break
                idx += 1
            if not matched:
                return False
        return True
    return expected == actual


def _normalize_inputs(test_case: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = test_case.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = test_case.get("input")
    if not isinstance(inputs, Mapping):
        return {"obs": {}, "state": {}}
    obs = inputs.get("obs")
    state = inputs.get("state")
    return {
        "obs": dict(obs) if isinstance(obs, Mapping) else {},
        "state": dict(state) if isinstance(state, Mapping) else {},
    }


def _normalize_expected(test_case: Mapping[str, Any]) -> Mapping[str, Any] | None:
    expected = test_case.get("expected")
    if not isinstance(expected, Mapping):
        expected = test_case.get("expect")
    if not isinstance(expected, Mapping):
        return None
    return expected


def run_test_suite(*, suite: Mapping[str, Any], policy: Mapping[str, Any]) -> PolicyRunOutcome:
    spec = suite.get("spec")
    if not isinstance(spec, Mapping):
        raise ValueError("suite.spec must be an object")
    tests = spec.get("tests")
    if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes, bytearray)):
        raise ValueError("suite.spec.tests must be an array")

    engine = PolicyEngine(policy)
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    warnings = 0
    blocking_failure = False

    for raw_test in tests:
        if not isinstance(raw_test, Mapping):
            continue
        test_id = str(raw_test.get("id", ""))
        stability = str(raw_test.get("stability", ""))
        intent_refs = raw_test.get("intent_refs")
        if not isinstance(intent_refs, Sequence) or isinstance(intent_refs, (str, bytes, bytearray)):
            intent_refs = []
        intent_refs_list = [str(item) for item in intent_refs]

        inputs = _normalize_inputs(raw_test)
        expected = _normalize_expected(raw_test)

        actual_log = engine.evaluate(obs=inputs["obs"], state=inputs["state"])
        status = "pass"
        reason = "ok"
        diff: dict[str, Any] | None = None

        if expected is None:
            status = "fail"
            reason = "missing expected/expect block"
            diff = {"actual": actual_log}
        else:
            mode = str(expected.get("mode", "exact"))
            expected_action_log = expected.get("action_log")
            if mode == "exact":
                if canonical_json_bytes(actual_log) != canonical_json_bytes(expected_action_log):
                    status = "fail"
                    reason = "exact mismatch"
                    diff = {"expected": expected_action_log, "actual": actual_log}
            elif mode == "contains":
                if not _match_contains(expected_action_log, actual_log):
                    status = "fail"
                    reason = "contains mismatch"
                    diff = {"expected": expected_action_log, "actual": actual_log}
            else:
                status = "fail"
                reason = f"unsupported expect mode '{mode}'"
                diff = {"expected": expected_action_log, "actual": actual_log}

        if status == "fail" and stability == "evolving":
            status = "warn"
            warnings += 1
        elif status == "fail":
            failed += 1
            if stability in {"hard", "compat"}:
                blocking_failure = True
        else:
            passed += 1

        entry: dict[str, Any] = {
            "test_id": test_id,
            "stability": stability,
            "intent_refs": intent_refs_list,
            "status": status,
            "reason": reason,
        }
        if diff is not None:
            entry["diff"] = diff
        results.append(entry)

    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
    }
    report: dict[str, Any] = {
        "summary": summary,
        "results": results,
    }
    return PolicyRunOutcome(exit_code=1 if blocking_failure else 0, report=report)


def run_test_suite_files(*, suite_path: Path, policy_path: Path) -> PolicyRunOutcome:
    suite = _load_mapping(suite_path)
    policy = _load_mapping(policy_path)
    return run_test_suite(suite=suite, policy=policy)


__all__ = ["PolicyRunOutcome", "run_test_suite", "run_test_suite_files"]
