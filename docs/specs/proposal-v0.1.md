# Proposal v0.1

**Version**: tracemind.io/v0.1  
**Status**: Specification (M6.1)  
**Scope**: Proposal ontology and semantic boundaries; minimal closed loop for patch proposals.

---

## 1. Scope & Non-goals

### In scope (v0.1)

- **Proposal envelope structure** following M1.1 canonical AST (`api_version`, `kind`, `metadata`, `spec`)
- **Minimal `spec` fields** sufficient for patch proposal lifecycle (draft → submit → approve → apply)
- **Lint vs schema boundaries**: what schema validation can catch vs what requires lint rules
- **Relationship to gate**: proposal is a candidate description; execution requires `tm gate proposal` (M6.2)

### Out of scope (v0.1)

- **Signatures/encryption/approval chains**: v0.1 does not define cryptographic signatures, multi-party approval workflows, or encrypted payloads. These are deferred to future versions.
- **Complex scope derivation**: v0.1 does not auto-derive `impacted_intents` from patch content; it must be explicitly declared.
- **Patch operation language**: v0.1 references patches by `ref` only; the patch operation format (add/remove/modify) is defined elsewhere.
- **Rendering syntax**: YAML/JSON are parse/print only; gate operates on canonical AST.

### Principle

A proposal is a **candidate description** of a change. It becomes actionable only after passing gate checks (`tm gate proposal`). The proposal itself does not execute; it describes what would change.

---

## 2. Proposal envelope

Proposal follows the **M1.1 canonical AST envelope** structure:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `api_version` | string | yes | Fixed `"tracemind.io/v0.1"` |
| `kind` | string | yes | Fixed `"Proposal"` |
| `metadata` | object | yes | See below |
| `spec` | object | yes | Proposal-specific payload; see §3 |

### `metadata` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | yes | Stable proposal identifier (e.g. `"prop-001"`, `"TM-PROP-2025-001"`) |
| `version` | string | yes | Semantic version or tag (e.g. `"1.0.0"`, `"v0"`) |
| `trace_links` | object | no | `parent_intent`, `related_intents` (set-like) |
| `labels` | object | no | `map<string, string>` for tagging |

The envelope structure is **closed** (`additionalProperties: false` at root and `metadata` level). Unknown keys are invalid.

---

## 3. Proposal.spec minimum fields (v0.1)

The `spec` object MUST contain these fields (v0.1 minimal set):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `impacted_intents` | array of string | yes | Non-empty list of intent IDs affected by this proposal. Must be explicitly declared (no auto-derivation in v0.1). |
| `patches` or `patch_refs` | array | yes | **Either** `patches: [{ ref: string }]` **or** `patch_refs: [string]`. Must be unique (no duplicate refs). References patch artifacts or patch operation files. |
| `tests` or `testsuite_refs` | array | yes | **Either** `tests: [{ ref: string }]` **or** `testsuite_refs: [string]`. References test suites or test files that cover this proposal. |
| `risk` | string | yes | Enum: `"low"` \| `"medium"` \| `"high"`. Risk level for the proposed change. |
| `summary` | string | yes | Short human-readable summary (one sentence or phrase). |
| `rationale` | string | no | Optional longer explanation of why this change is needed. |

### Field semantics

- **`impacted_intents`**: Must be non-empty. Each string is an intent ID (e.g. `"TM-INT-0001"`). Used for traceability and coverage checks.
- **`patches` / `patch_refs`**: Choose one naming style and use consistently. If using `patches`, each element is `{ ref: string }`. If using `patch_refs`, it's a flat array of strings. Refs must be unique within the array.
- **`tests` / `testsuite_refs`**: Same pattern as patches. Refs point to test suites or test files that validate the proposal.
- **`risk`**: Required enum. Used by gate to determine approval requirements or blocking rules.
- **`summary`**: Required short description. Used in reports and UI.
- **`rationale`**: Optional detailed explanation. Helps reviewers understand the proposal context.

---

## 4. Rendering layer

**YAML and JSON are parse/print only**. The canonical form is JSON AST. Gate, lint, and validation operate on the AST, not on the file format.

- **Parse**: YAML/JSON → canonical AST (JSON object)
- **Print**: canonical AST → YAML/JSON (for human readability)
- **Gate**: operates on AST only

This means:
- Whitespace, comments, key order in YAML do not affect gate behavior
- Only the AST structure (fields, types, values) matters
- File format is a presentation concern

---

## 5. Lint vs Schema boundaries

| Check | Schema (JSON Schema) | Lint (tm lint / gate) |
|-------|---------------------|----------------------|
| **Required fields** | ✅ `required: ["impacted_intents", "risk", "summary"]` | — |
| **Type validation** | ✅ `type: "string"`, `type: "array"` | — |
| **Enum values** | ✅ `enum: ["low", "medium", "high"]` for `risk` | — |
| **Additional properties** | ✅ `additionalProperties: false` at envelope/metadata | — |
| **Non-empty array** | ✅ `minItems: 1` for `impacted_intents` | — |
| **Reference existence** | ❌ | ✅ Lint checks that `patches[*].ref` or `patch_refs[*]` point to existing files/artifacts |
| **Intent ID validity** | ❌ | ✅ Lint checks that `impacted_intents[*]` are valid intent IDs (exist in intent tree) |
| **Ref uniqueness** | ❌ | ✅ Lint checks that patch refs and test refs are unique within their arrays |
| **Scope constraints** | ❌ | ✅ Lint may enforce that `impacted_intents` match the scope of referenced patches |
| **Authorization** | ❌ | ✅ Gate checks that proposer has permission to modify referenced intents/artifacts |

**Schema** catches structural errors (missing fields, wrong types, invalid enums). **Lint** catches semantic errors (broken references, invalid IDs, authorization violations).

---

## 6. Relationship to gate

A proposal is a **candidate description**. It does not execute changes by itself.

- **Proposal creation**: Creates a draft proposal artifact (status: `DRAFT`).
- **Proposal submission**: Moves status to `SUBMITTED`; triggers gate checks.
- **Gate execution**: `tm gate proposal <proposal_id>` runs:
  - Schema validation
  - Lint checks (references, intent IDs, uniqueness)
  - Authorization checks
  - Risk assessment
  - Returns gate decision (allow/deny) with reasons
- **Approval/application**: Only after gate allows, the proposal can be approved and applied (M6.2+).

**v0.1 does not define the gate implementation**; it only defines the proposal structure that gate consumes.

---

## 7. Minimal proposal example

```json
{
  "api_version": "tracemind.io/v0.1",
  "kind": "Proposal",
  "metadata": {
    "id": "prop-001",
    "version": "1.0.0",
    "trace_links": {
      "related_intents": ["TM-INT-0001"]
    }
  },
  "spec": {
    "impacted_intents": ["TM-INT-0001", "TM-INT-0002"],
    "patch_refs": ["patches/tighten-guard-v1.yaml"],
    "testsuite_refs": ["tests/policy/test_tighten_guard.py"],
    "risk": "medium",
    "summary": "Tighten guard on state:workload writes",
    "rationale": "Current guard allows writes that violate intent TM-INT-0001 constraints. This patch adds an additional policy rule to block unsafe writes."
  }
}
```

### Field explanations

- **`impacted_intents`**: Required, non-empty. Lists intent IDs (`TM-INT-0001`, `TM-INT-0002`) that this proposal affects. Used for traceability and coverage checks.
- **`patch_refs`**: Required. Array of patch references. Using `patch_refs` (flat strings) instead of `patches: [{ ref }]` for simplicity in v0.1.
- **`testsuite_refs`**: Required. Array of test suite references that validate this proposal.
- **`risk`**: Required enum. `"medium"` indicates moderate risk; gate may require additional review.
- **`summary`**: Required short description. Used in reports and UI.
- **`rationale`**: Optional detailed explanation. Helps reviewers understand why this change is needed.

---

## 8. Field naming and extensibility

### Naming convention

- **snake_case** for all field names (e.g. `impacted_intents`, `patch_refs`, `testsuite_refs`).
- Consistent with M1.1 envelope (`api_version`, `trace_links`).

### Version bump strategy

- **v0.1 → v0.2**: Additive changes only (new optional fields, new enum values). Old proposals remain valid.
- **v0.x → v1.0**: Breaking changes (required fields added, field renames, structural changes). Requires migration or explicit version handling.

### Extensibility points (future)

- **Approval chain**: Add `approvals: [{ actor, timestamp, signature }]` (v0.2+)
- **Scope derivation**: Add `auto_derive_impacted_intents: bool` (v0.2+)
- **Patch operations**: Expand `patches` to include inline operations (v0.2+)
- **Risk details**: Expand `risk` to include structured risk assessment (v0.2+)

These are **not** in v0.1; they are listed here to show how the spec can evolve without breaking existing proposals.
