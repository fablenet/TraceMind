# Policy determinism (M2 · ISSUE-003)

**Status**: Canonical semantics. These rules are immutable to avoid implementation drift ("语义宪法化").

This document defines the deterministic semantics of policy evaluation and the canonical `action_log` format. All implementations (runtime, tests, tooling) must conform.

**Summary (five invariants):** (1) **Rule order** — priority DESC, then id ASC; actions within a rule keep original order. (2) **Conflict** — same actuator multiple `set`: first-wins, later entries recorded with `applied: false`. (3) **Missing field** — missing var path resolves to `None`. (4) **Type mismatch** — `None` or incompatible types in comparisons yield `False`. (5) **Canonical action_log** — JSON with `sort_keys=True`, fixed separators `(",", ":")`; array order by evaluation only; no non-deterministic fields.

---

## 1. Rule evaluation order

- **Rules** are ordered by: **priority DESC**, then **id ASC** (string comparison).
- Only rules whose `when` condition evaluates to true are considered.
- Within a rule, **actions** in `then` keep their **original order** (no reordering).

Example: with rules `(id=z, priority=15)`, `(id=a, priority=15)`, `(id=high, priority=20)`, the evaluation order is: `high` → `a` → `z`.

---

## 2. Conflict strategy (same actuator, multiple `set`)

- For actions of type **`set`**, the **actuator** is the key: at most one `set` per actuator is **applied**.
- **First-wins**: the first (by evaluation order) `set` that writes to an actuator is applied; its value is committed to the effective patch.
- Any **later** `set` to the same actuator is **still recorded** in the action log but with **`applied: false`** (conflict; no effect on the patch).
- Non-`set` actions (e.g. `emit_event`) do not conflict; they are always recorded with `applied: true` when the rule fires.

---

## 3. Condition AST semantics

- **Logical operators**: `all`, `any`, `not`. Operands are sub-conditions (recursive).
- **Comparison operators** (exactly 6): `==`, `!=`, `>`, `>=`, `<`, `<=`. Operands: `[left, right]`; either may be a literal or `{"var": "path"}`.
- **Variable path**: `{"var": "path"}` with dot-separated path (e.g. `obs.temp`, `state.mode`). Lookup is against an env with top-level keys `obs` and `state`.
- **Missing var**: If the path does not exist or any segment is missing, the resolved value is **`None`**.
- **None or type mismatch**: Any comparison where one side is `None`, or where the two sides are not type-compatible (e.g. number vs string), yields **`False`** (the condition does not hold).
- No other operators or fields are allowed for conditions; malformed conditions evaluate to `False`.

---

## 4. Canonical action_log (JSON)

The **action_log** (e.g. policy evaluation result) must be serialized in a **canonical** form so that two equivalent evaluations produce identical bytes.

- **JSON serialization**:
  - **`sort_keys=True`**
  - **Fixed separators**: `(",", ":")` (no space after colon/comma).
- **Array order**: The `actions` array order is **fully determined by evaluation order** (rules by priority DESC, id ASC; then actions in rule order). No arbitrary reordering.
- **No non-deterministic fields**: Do not add timestamps, random IDs, or any other non-deterministic fields to the action_log. Only deterministic inputs (policy, obs, state) may affect the output.

Canonical form example (conceptually):

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

---

## 5. Minimal example: policy + obs/state → action_log (with `applied: false` conflict)

**Input**

- **Policy** (rules evaluated in order: rule-high, rule-low):

```json
{
  "rules": [
    {
      "id": "rule-low",
      "priority": 10,
      "when": { "==": [{"var": "obs.flag"}, true] },
      "then": [{ "type": "set", "actuator": "motor.speed", "value": 1 }]
    },
    {
      "id": "rule-high",
      "priority": 20,
      "when": { "==": [{"var": "obs.flag"}, true] },
      "then": [{ "type": "set", "actuator": "motor.speed", "value": 9 }]
    }
  ]
}
```

- **obs**: `{"flag": true}`
- **state**: `{}`

**Output (action_log)**

- rule-high runs first (priority 20 > 10); its `set motor.speed = 9` is applied.
- rule-low runs second; its `set motor.speed = 1` conflicts (actuator already set) → recorded with `applied: false`.

Canonical JSON (compact, sort_keys, separators `(",", ":")`):

```json
{"actions":[{"action":{"actuator":"motor.speed","type":"set","value":9},"applied":true,"rule_id":"rule-high"},{"action":{"actuator":"motor.speed","type":"set","value":1},"applied":false,"rule_id":"rule-low"}],"final_patch":{"motor.speed":9}}
```

Decoded for readability:

```json
{
  "actions": [
    {
      "rule_id": "rule-high",
      "action": { "type": "set", "actuator": "motor.speed", "value": 9 },
      "applied": true
    },
    {
      "rule_id": "rule-low",
      "action": { "type": "set", "actuator": "motor.speed", "value": 1 },
      "applied": false
    }
  ],
  "final_patch": { "motor.speed": 9 }
}
```

This is the **immutable** contract for M2 policy evaluation and action_log; implementations must not deviate.
