# Trace format v0.1 (JSONL event structure)

This spec defines the **canonical AST** for trace JSONL: one JSON object per line. It does not define rendering or display.

---

## 1. Event structure (per line)

Each line of a trace JSONL file is a single JSON object representing one **input event** to the policy/runtime. The canonical shape is:

| Field     | Type   | Required | Description |
|----------|--------|----------|-------------|
| `ts`     | string | no       | Timestamp (e.g. RFC 3339). Replay does **not** depend on it; present only for ordering or audit. |
| `obs`    | object | yes      | Observations (e.g. sensors, request payload). Key-value; structure is domain-specific. |
| `state`  | object | yes      | Current state (e.g. mode, flags). Key-value; structure is domain-specific. |
| `context`| object | no       | Optional context for request_id, actor, env. |

### 1.1. `context` (optional)

When present, recommended subfields:

| Subfield      | Type   | Description |
|---------------|--------|-------------|
| `request_id`  | string | Idempotency or correlation ID for the request. |
| `actor`       | string | Identity (user, service, agent) that triggered the event. |
| `env`         | string | Environment label (e.g. `prod`, `staging`). |

Other keys are permitted for tooling; replay semantics depend only on `obs` and `state`.

### 1.2. Canonical form

- One event per line; no multi-line JSON within a single record.
- Encoding: UTF-8.
- For deterministic comparison, event objects should be serialized with a fixed convention (e.g. `sort_keys=True`, `separators=(",", ":")`) when writing or diffing; this spec does not mandate a single serialization for the file on disk, but tooling that compares traces must use a canonical form.

---

## 2. Replay output

**Replay** means: given a trace JSONL (sequence of input events) and a policy, evaluate the policy for each event and produce an output per event.

- **Input**: Trace JSONL (each line = one event with at least `obs`, `state`).
- **Output**: For each input event, one **action_log**.
- The **action_log** format is the canonical policy evaluation result defined in [Policy determinism (M2)](../policy_determinism.md): `actions` (array of `{ rule_id, action, applied }`), `final_patch`, and canonical JSON serialization (`sort_keys=True`, fixed separators, no non-deterministic fields). Replay must not rely on `ts`; only `obs` and `state` drive the result.

Output shape (conceptual): a sequence of `action_log` objects, one-to-one with input lines, e.g. same order as the input trace or an array/keyed by line index.

---

## 3. Replay diff output

**Replay diff** compares two replay runs (e.g. baseline vs candidate policy, or same policy on two traces) and reports differences in the produced action_logs.

### 3.1. Classification

Each difference is classified as:

| Category   | Meaning |
|------------|--------|
| **added**  | Present in the candidate run but not in the baseline (e.g. new rule fired, new action). |
| **removed**| Present in the baseline but not in the candidate (e.g. rule no longer fires, action dropped). |
| **changed**| Same rule/action key but different value or metadata (e.g. `applied` flipped, different `final_patch` value). |

### 3.2. Summary dimension

Differences are summarized along a **primary dimension** for reporting:

- **Preferred**: By **intent_refs** — if the action_log (or policy metadata) can carry a mapping from `rule_id` to `intent_refs`, the diff report SHOULD aggregate by `intent_refs` so that regressions can be attributed to intents.
- **Fallback**: If that mapping is not available, aggregate by **rule_id** (and optionally by event index / request_id). Implementations MUST support at least rule_id-based aggregation.

So the diff output structure should allow:

- Per-event or per-line diff (which events/lines had added/removed/changed).
- A summary that groups by `intent_refs` when available, else by `rule_id`, with counts or lists for added/removed/changed.

Example summary shape (conceptual):

```json
{
  "by_intent_refs": { "intent/A": { "added": 2, "removed": 0, "changed": 1 }, ... },
  "by_rule_id":    { "rule-high": { "added": 1, "removed": 0, "changed": 1 }, ... },
  "events": [ { "index": 0, "added": [...], "removed": [...], "changed": [...] }, ... ]
}
```

Implementations may add fields (e.g. request_id, ts) for audit; the minimum required for v0.1 is the three categories (added/removed/changed) and a summary dimension (intent_refs when possible, else rule_id).
