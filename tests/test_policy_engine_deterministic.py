from __future__ import annotations

import json
from pathlib import Path

from tm.policy import PolicyEngine, canonical_json_bytes, evaluate_condition


def test_condition_ast_boundary_and_nested_not() -> None:
    env = {
        "obs": {"temp": 50, "humidity": 20},
        "state": {"blocked": False, "mode": "AUTO"},
    }
    cond = {
        "all": [
            {">=": [{"var": "obs.temp"}, 50]},
            {"not": {"not": {"==": [{"var": "state.mode"}, "AUTO"]}}},
            {"not": {"==": [{"var": "state.blocked"}, True]}},
        ]
    }
    assert evaluate_condition(cond, env) is True


def test_condition_ast_missing_field_is_false() -> None:
    env = {"obs": {"temp": 30}, "state": {}}
    cond = {"==": [{"var": "state.missing.value"}, "X"]}
    assert evaluate_condition(cond, env) is False


def test_condition_ast_type_mismatch_is_false() -> None:
    env = {"obs": {"temp": "50"}, "state": {}}
    assert evaluate_condition({">=": [{"var": "obs.temp"}, 50]}, env) is False
    assert evaluate_condition({"==": [{"var": "obs.temp"}, 50]}, env) is False


def test_policy_engine_determinism_bytes_equal() -> None:
    policy = {
        "rules": [
            {
                "id": "rule-low",
                "priority": 10,
                "when": {">": [{"var": "obs.temp"}, 10]},
                "then": [{"type": "set", "actuator": "fan", "value": "low"}],
            },
            {
                "id": "rule-high",
                "priority": 30,
                "when": {">": [{"var": "obs.temp"}, 10]},
                "then": [{"type": "set", "actuator": "fan", "value": "high"}],
            },
        ]
    }
    engine = PolicyEngine(policy)
    first = canonical_json_bytes(engine.evaluate(obs={"temp": 20}, state={}))
    second = canonical_json_bytes(engine.evaluate(obs={"temp": 20}, state={}))
    assert first == second


def test_policy_engine_conflict_first_wins_by_priority() -> None:
    policy = {
        "rules": [
            {
                "id": "rule-low",
                "priority": 10,
                "when": {"==": [{"var": "obs.flag"}, True]},
                "then": [{"type": "set", "actuator": "motor.speed", "value": 1}],
            },
            {
                "id": "rule-high",
                "priority": 20,
                "when": {"==": [{"var": "obs.flag"}, True]},
                "then": [{"type": "set", "actuator": "motor.speed", "value": 9}],
            },
        ]
    }
    output = PolicyEngine(policy).evaluate(obs={"flag": True}, state={})
    actions = output["actions"]
    assert actions[0]["rule_id"] == "rule-high"
    assert actions[0]["applied"] is True
    assert actions[1]["rule_id"] == "rule-low"
    assert actions[1]["applied"] is False
    assert output["final_patch"]["motor.speed"] == 9


def test_policy_engine_conflict_first_wins_by_id_when_same_priority() -> None:
    policy = {
        "rules": [
            {
                "id": "z-last",
                "priority": 15,
                "when": {"==": [{"var": "obs.ok"}, True]},
                "then": [{"type": "set", "actuator": "alarm.state", "value": "on"}],
            },
            {
                "id": "a-first",
                "priority": 15,
                "when": {"==": [{"var": "obs.ok"}, True]},
                "then": [{"type": "set", "actuator": "alarm.state", "value": "off"}],
            },
        ]
    }
    output = PolicyEngine(policy).evaluate(obs={"ok": True}, state={})
    actions = output["actions"]
    assert actions[0]["rule_id"] == "a-first"
    assert actions[0]["applied"] is True
    assert actions[1]["rule_id"] == "z-last"
    assert actions[1]["applied"] is False
    assert output["final_patch"]["alarm.state"] == "off"


def test_policy_engine_non_conflict_all_applied() -> None:
    policy = {
        "rules": [
            {
                "id": "rule-a",
                "priority": 5,
                "when": {"==": [{"var": "obs.go"}, True]},
                "then": [{"type": "set", "actuator": "pump", "value": "on"}],
            },
            {
                "id": "rule-b",
                "priority": 4,
                "when": {"==": [{"var": "obs.go"}, True]},
                "then": [{"type": "set", "actuator": "fan", "value": "high"}],
            },
        ]
    }
    output = PolicyEngine(policy).evaluate(obs={"go": True}, state={})
    assert [entry["applied"] for entry in output["actions"]] == [True, True]
    assert output["final_patch"] == {"pump": "on", "fan": "high"}


def test_policy_engine_golden_snapshot() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "policy" / "v0.1" / "action_log_golden.json"
    expected = fixture.read_bytes()
    policy = {
        "rules": [
            {
                "id": "rule-b",
                "priority": 10,
                "when": {">=": [{"var": "obs.temp"}, 50]},
                "then": [
                    {"type": "set", "actuator": "fan.mode", "value": "eco"},
                    {"type": "emit_event", "event": "fan-adjusted", "payload": {"from": "policy"}},
                ],
            },
            {
                "id": "rule-a",
                "priority": 20,
                "when": {">": [{"var": "obs.humidity"}, 70]},
                "then": [{"type": "set", "actuator": "fan.mode", "value": "turbo"}],
            },
            {
                "id": "rule-c",
                "priority": 10,
                "when": {"==": [{"var": "state.mode"}, "AUTO"]},
                "then": [{"type": "set", "actuator": "pump.state", "value": "on"}],
            },
        ]
    }
    output = PolicyEngine(policy).evaluate(obs={"temp": 60, "humidity": 80}, state={"mode": "AUTO"})
    actual = canonical_json_bytes(output)
    assert actual == expected
    # Keep a decoded assertion to make failures readable.
    assert json.loads(actual.decode("utf-8"))["final_patch"] == {"fan.mode": "turbo", "pump.state": "on"}
