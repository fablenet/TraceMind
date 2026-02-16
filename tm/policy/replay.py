from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from tm.policy.deterministic import PolicyEngine, canonical_json_bytes
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


def replay_trace(*, trace_path: Path, policy_path: Path, out_path: Path) -> int:
    rows = replay_trace_rows(trace_path=trace_path, policy_path=policy_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as out_handle:
        for row in rows:
            out_handle.write(canonical_json_bytes(row).decode("utf-8"))
            out_handle.write("\n")
    return len(rows)


def replay_trace_rows(*, trace_path: Path, policy_path: Path) -> list[dict[str, Any]]:
    policy = _load_mapping(policy_path)
    engine = PolicyEngine(policy)
    rows: list[dict[str, Any]] = []
    with trace_path.open("r", encoding="utf-8") as in_handle:
        for index, raw in enumerate(in_handle):
            line = raw.strip()
            if not line:
                continue
            event = json.loads(line)
            if not isinstance(event, Mapping):
                raise ValueError(f"{trace_path}: line {index + 1} must be a JSON object")
            obs = event.get("obs")
            state = event.get("state")
            obs_obj = dict(obs) if isinstance(obs, Mapping) else {}
            state_obj = dict(state) if isinstance(state, Mapping) else {}
            action_log = engine.evaluate(obs=obs_obj, state=state_obj)
            input_payload = {"obs": obs_obj, "state": state_obj}
            input_hash = hashlib.sha256(canonical_json_bytes(input_payload)).hexdigest()
            rows.append(
                {
                    "index": index,
                    "input_hash": input_hash,
                    "action_log": action_log,
                }
            )
    return rows


def _load_jsonl_rows(path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError(f"{path}: line {line_no} must be a JSON object")
            rows.append(payload)
    return rows


def _action_diff(old_actions: Sequence[Any], new_actions: Sequence[Any]) -> dict[str, Any]:
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    common = min(len(old_actions), len(new_actions))
    for idx in range(common):
        if canonical_json_bytes(old_actions[idx]) != canonical_json_bytes(new_actions[idx]):
            modified.append({"index": idx, "old": old_actions[idx], "new": new_actions[idx]})
    for idx in range(common, len(old_actions)):
        removed.append({"index": idx, "action": old_actions[idx]})
    for idx in range(common, len(new_actions)):
        added.append({"index": idx, "action": new_actions[idx]})
    return {"added": added, "removed": removed, "modified": modified}


def diff_replay_rows(
    *, old_rows: Sequence[Mapping[str, Any]], new_rows: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    old_by_index = {int(row.get("index", idx)): row for idx, row in enumerate(old_rows)}
    new_by_index = {int(row.get("index", idx)): row for idx, row in enumerate(new_rows)}
    all_indices = sorted(set(old_by_index.keys()) | set(new_by_index.keys()))

    rows: list[dict[str, Any]] = []
    changed_rows = 0
    added_rows = 0
    removed_rows = 0
    by_rule: dict[str, dict[str, int]] = {}

    def bump_rule(rule_id: str, field: str) -> None:
        if rule_id not in by_rule:
            by_rule[rule_id] = {
                "changed_rows": 0,
                "action_added": 0,
                "action_removed": 0,
                "action_modified": 0,
            }
        by_rule[rule_id][field] += 1

    for index in all_indices:
        old_row = old_by_index.get(index)
        new_row = new_by_index.get(index)
        if old_row is None:
            added_rows += 1
            changed_rows += 1
            rows.append({"index": index, "status": "added", "new": new_row})
            continue
        if new_row is None:
            removed_rows += 1
            changed_rows += 1
            rows.append({"index": index, "status": "removed", "old": old_row})
            continue

        old_log = old_row.get("action_log")
        new_log = new_row.get("action_log")
        if canonical_json_bytes(old_log) == canonical_json_bytes(new_log):
            rows.append({"index": index, "status": "same"})
            continue

        changed_rows += 1
        old_actions = old_log.get("actions") if isinstance(old_log, Mapping) else []
        new_actions = new_log.get("actions") if isinstance(new_log, Mapping) else []
        old_actions_list = (
            list(old_actions)
            if isinstance(old_actions, Sequence) and not isinstance(old_actions, (str, bytes, bytearray))
            else []
        )
        new_actions_list = (
            list(new_actions)
            if isinstance(new_actions, Sequence) and not isinstance(new_actions, (str, bytes, bytearray))
            else []
        )
        actions_diff = _action_diff(old_actions_list, new_actions_list)

        touched_rules: set[str] = set()
        for entry in actions_diff["added"]:
            action = entry.get("action")
            if isinstance(action, Mapping):
                rule_id = str(action.get("rule_id", ""))
                if rule_id:
                    touched_rules.add(rule_id)
                    bump_rule(rule_id, "action_added")
        for entry in actions_diff["removed"]:
            action = entry.get("action")
            if isinstance(action, Mapping):
                rule_id = str(action.get("rule_id", ""))
                if rule_id:
                    touched_rules.add(rule_id)
                    bump_rule(rule_id, "action_removed")
        for entry in actions_diff["modified"]:
            old_action = entry.get("old")
            new_action = entry.get("new")
            for candidate in (old_action, new_action):
                if isinstance(candidate, Mapping):
                    rule_id = str(candidate.get("rule_id", ""))
                    if rule_id:
                        touched_rules.add(rule_id)
                        bump_rule(rule_id, "action_modified")
                        break
        for rule_id in touched_rules:
            bump_rule(rule_id, "changed_rows")

        rows.append(
            {
                "index": index,
                "status": "changed",
                "diff": {
                    "actions": actions_diff,
                    "action_log_old": old_log,
                    "action_log_new": new_log,
                },
            }
        )

    report = {
        "summary": {
            "total_rows_old": len(old_rows),
            "total_rows_new": len(new_rows),
            "changed_rows": changed_rows,
            "added_rows": added_rows,
            "removed_rows": removed_rows,
        },
        "by_rule": [
            {"rule_id": rule_id, **stats} for rule_id, stats in sorted(by_rule.items(), key=lambda item: item[0])
        ],
        "rows": rows,
    }
    return report


def diff_replay_files(*, old_path: Path, new_path: Path) -> Mapping[str, Any]:
    old_rows = _load_jsonl_rows(old_path)
    new_rows = _load_jsonl_rows(new_path)
    return diff_replay_rows(old_rows=old_rows, new_rows=new_rows)


__all__ = ["replay_trace", "replay_trace_rows", "diff_replay_rows", "diff_replay_files"]
