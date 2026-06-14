# TraceMind K-Ontology v0.4 — IntentSession additive upgrade

**Version**: tracemind.io/v0.4
**Status**: Specification (Phase 7 Stage 7-2)
**Scope**: Additive upgrade of [K-Ontology v0.3](k-ontology-v0.3.md). Introduces one new artifact kind, **`IntentSession`** — a persistent, versioned *design journal* that records how a requirement is iteratively turned into formal, verifiable artifacts. v0.3 ➜ v0.4 is purely additive: every v0.1/v0.2/v0.3 artifact remains valid under v0.4 schemas.

---

## 1. Scope & Non-Goals

### In scope (v0.4)

- New artifact kind: **`IntentSession`** — the durable state of the iterative NL→Formal design loop: an append-only `turns` journal, the current design-loop step, an embedded latest 5W1H completeness snapshot, the produced formal artifact refs, and (once sealed) an accountable `sign_off`.
- v0.3 → v0.4 backward compatibility: **every existing artifact remains valid under v0.4 schemas** (one new `artifact_type` enum value, one new body schema; no envelope or canonicalization changes, no existing schema field changes).

### Out of scope (v0.4 / Stage 7-2.1)

- **Design-loop transition gating** — the legal `current_step` transitions, entry gates, and human-only steps are the frozen contract in `tm/intent/design_loop.py` (Task 7-5.0). Enforcing them during `refine` is Stage 7-2.2.
- **Deterministic refine** — the fake/rule-based multi-turn refinement that appends turns is Stage 7-2.4.
- **Seal-time uncertainty closure** — `IntentSession` only requires a `sign_off` to *exist* when `status=sealed`. Verifying that every `partial`/`missing` 5W1H dimension is `resolved`/`waived`/`dynamic` (per `tm/intent/uncertainty.py`) is Stage 7-2.8.
- **Equivalence semantics** — byte-identical equivalence is defined on the *frozen formal products* (Intent / PatternInstance / Bundle) referenced by `produced_refs`, never on the mutable journal itself.

### Principle

v0.4 is purely **additive**:

- One new value in `ArtifactType` enum: `intent_session`
- One new artifact body schema: `schemas/v0/intent_session.json`
- One new in-runtime AST schema: `IntentSessionSpec` in `tm/artifacts/schema.py`
- No envelope structural changes; no canonicalization changes; no existing schema field changes

---

## 2. New artifact kind: `IntentSession`

An IntentSession is a **first-class, mutable working artifact**. Unlike the immutable formal products, it is expected to evolve (`status=working`) across many design turns and is finally frozen (`status=sealed`). It **references** the formal products it produces by `artifact_id` — it never embeds them.

### Body schema (v0.4 form)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `session_id` | string | yes | Stable identifier; matches `^[a-z0-9]+(?:[._-][a-z0-9]+)*$`, e.g. `"session.anon_fairness.v1"` |
| `root_intent_ref` | string | yes | Artifact ID of the Intent under design |
| `status` | enum: `working` \| `sealed` | yes | Session lifecycle. Mirrors `SessionStatus` in `design_loop.py` |
| `current_step` | enum | yes | One of `draft` \| `check_5w1h` \| `propose` \| `refine` \| `verify` \| `accept` \| `sealed`. Mirrors `DesignStep` |
| `turns` | array of `Turn` | no | Append-only journal (defaults to empty). `seq` MUST be strictly increasing |
| `completeness` | object | no | Embedded latest 5W1H completeness report snapshot (free-form; produced by `tm/intent/completeness.py`) |
| `produced_refs` | array of string | no | Artifact IDs of frozen formal products this session produced |
| `sign_off` | `SignOff` | conditionally | REQUIRED when `status=sealed`; absent while `working` |
| `metadata` | object | no | Free-form attribution / tags |

#### `Turn` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `seq` | integer (≥0) | yes | Monotonic journal index; strictly increasing across `turns` |
| `role` | enum: `human` \| `agent` | yes | Who took the turn |
| `action` | enum | yes | One of `propose` \| `refine` \| `check_5w1h` \| `verify` \| `accept` \| `clarify` \| `note`. Mirrors `TurnAction` |
| `input_ref` | string | no | Artifact / blob ref consumed by this turn |
| `output_ref` | string | no | Artifact / blob ref produced by this turn |
| `provider` | string | no | Provider tag for `agent` turns (e.g. `fake`, `openai`); deterministic paths use `fake` |
| `turn_hash` | string | no | Canonical hash of the turn payload (populated by the workbench; reserved for replay/audit) |

#### `SignOff` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `signer` | string | yes | Accountable human identity that sealed the session |
| `scope` | array of string | no | `produced_refs` covered by the sign-off |
| `completeness_snapshot` | object | no | The seal-mode 5W1H snapshot (incl. `profile_id`) at seal time |
| `dispositions` | object | no | Per-dimension uncertainty closure (`resolved`/`waived`/`dynamic`); validated in Stage 7-2.8 |
| `gate_report_hash` | string | no | Hash of the design-time consistency gate report |
| `signed_at` | string (date-time) | no | Seal timestamp |
| `sign_hash` | string | no | Canonical hash of the sign-off record |

---

## 3. Lifecycle verification (Stage 7-2.1)

`verify()` accepts an `IntentSession` candidate via two layers (full transition gating arrives in Stage 7-2.2):

1. **JSON schema** — `validate_intent_session_spec` enforces required fields and the `status` / `current_step` / `Turn.action` / `Turn.role` enums.
2. **Journal + seal structure**
   - `turns[*].seq` MUST be strictly increasing.
   - A `sealed` session MUST carry a `sign_off` record.
3. **Body-hash determinism** — same canonical `body_hash` as every other artifact body.

The step / action / status vocabularies are **not** redefined here: they are the single frozen source of truth in `tm/intent/design_loop.py`. The body model mirrors those values as local frozensets, and tests assert they never drift.

---

## 4. Backward compatibility

v0.3 → v0.4 introduces only one new `artifact_type` value and one new body schema. There are **no** changes to the envelope, canonicalization, body hashing, or any existing body schema. Every v0.1/v0.2/v0.3 artifact verifies byte-identically under v0.4 (see `tests/test_v03_compat_under_v04.py`).
