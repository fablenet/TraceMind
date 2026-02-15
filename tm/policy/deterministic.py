from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_LOGICAL_OPS = {"all", "any", "not"}
_COMPARE_OPS = {"==", "!=", ">", ">=", "<", "<="}


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _get_path(env: Mapping[str, Any], path: str) -> Any:
    current: Any = env
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def _resolve_value(value: Any, env: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping) and set(value.keys()) == {"var"} and isinstance(value.get("var"), str):
        return _get_path(env, value["var"])
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _types_compatible(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if _is_number(left) and _is_number(right):
        return True
    if isinstance(left, bool) and isinstance(right, bool):
        return True
    return type(left) is type(right)


def _compare(op: str, left: Any, right: Any) -> bool:
    if not _types_compatible(left, right):
        return False
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    return False


def evaluate_condition(condition: Any, env: Mapping[str, Any]) -> bool:
    if not isinstance(condition, Mapping) or len(condition) != 1:
        return False
    op, value = next(iter(condition.items()))
    if op in _LOGICAL_OPS:
        if op == "all":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
                return False
            return all(evaluate_condition(item, env) for item in value)
        if op == "any":
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
                return False
            return any(evaluate_condition(item, env) for item in value)
        if op == "not":
            return not evaluate_condition(value, env)
    if op in _COMPARE_OPS:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) != 2:
            return False
        left = _resolve_value(value[0], env)
        right = _resolve_value(value[1], env)
        return _compare(op, left, right)
    return False


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    priority: int
    when: Mapping[str, Any]
    actions: tuple[Mapping[str, Any], ...]


def _normalize_actions(then: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(then, Mapping) and isinstance(then.get("actions"), Sequence):
        raw_actions = then["actions"]
    elif isinstance(then, Sequence) and not isinstance(then, (str, bytes, bytearray)):
        raw_actions = then
    elif isinstance(then, Mapping):
        raw_actions = [then]
    else:
        raw_actions = []
    normalized: list[Mapping[str, Any]] = []
    for item in raw_actions:
        if isinstance(item, Mapping):
            normalized.append(dict(item))
    return tuple(normalized)


class PolicyEngine:
    def __init__(self, policy: Mapping[str, Any]):
        self._rules = self._normalize_rules(policy.get("rules"))

    @staticmethod
    def _normalize_rules(rules: Any) -> tuple[_Rule, ...]:
        if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes, bytearray)):
            return tuple()
        normalized: list[_Rule] = []
        for raw in rules:
            if not isinstance(raw, Mapping):
                continue
            rule_id = str(raw.get("id", ""))
            if not rule_id:
                continue
            priority_raw = raw.get("priority", 0)
            try:
                priority = int(priority_raw)
            except (TypeError, ValueError):
                priority = 0
            when = raw.get("when")
            if not isinstance(when, Mapping):
                continue
            actions = _normalize_actions(raw.get("then"))
            normalized.append(_Rule(rule_id=rule_id, priority=priority, when=when, actions=actions))
        normalized.sort(key=lambda rule: (-rule.priority, rule.rule_id))
        return tuple(normalized)

    @staticmethod
    def _normalize_action(action: Mapping[str, Any]) -> Mapping[str, Any]:
        action_type = str(action.get("type", ""))
        if action_type == "set":
            return {
                "type": "set",
                "actuator": action.get("actuator"),
                "value": action.get("value"),
            }
        if action_type == "emit_event":
            return {
                "type": "emit_event",
                "event": action.get("event"),
                "payload": action.get("payload"),
            }
        return {"type": action_type}

    def evaluate(self, *, obs: Mapping[str, Any], state: Mapping[str, Any]) -> Mapping[str, Any]:
        env: dict[str, Any] = {"obs": dict(obs), "state": dict(state)}
        actions: list[dict[str, Any]] = []
        final_patch: dict[str, Any] = {}
        for rule in self._rules:
            if not evaluate_condition(rule.when, env):
                continue
            for raw_action in rule.actions:
                action = self._normalize_action(raw_action)
                action_type = action.get("type")
                applied = True
                if action_type == "set":
                    actuator = action.get("actuator")
                    if not isinstance(actuator, str):
                        applied = False
                    elif actuator in final_patch:
                        applied = False
                    else:
                        final_patch[actuator] = action.get("value")
                entry = {"rule_id": rule.rule_id, "action": action, "applied": applied}
                actions.append(entry)
        return {"actions": actions, "final_patch": final_patch}


__all__ = ["PolicyEngine", "canonical_json_bytes", "evaluate_condition"]
