# TraceMind K-Ontology v0.2 — PropertyPattern additive upgrade

**Version**: tracemind.io/v0.2  
**Status**: Specification (Phase 5 Stage 5-1)  
**Scope**: Additive upgrade of [K-Ontology v0.1](k-ontology-v0.1.md). Introduces a new artifact kind for **reusable formal property patterns** (safety / liveness / fairness), and adds optional fields to IntentTree for referencing pattern instances.

---

## 1. Scope & Non-Goals

### In scope (v0.2)

- New artifact kind: **`PropertyPattern`** — a reusable, schema-locked formal template (CTL / LTL formula with typed slots) usable across domains
- IntentTree additive extension: optional `property_pattern_refs: [string]` and `slot_fills: object` fields that let an Intent **declare which patterns it implements** and how to fill their slots
- v0.1 → v0.2 backward compatibility: **every v0.1 artifact remains valid under v0.2 schemas** (no breaking changes; only optional fields and one new artifact_type enum value)

### Out of scope (v0.2)

- **Pattern execution semantics**: the meaning of CTL / LTL formulas, model checking, Kripke verification — all owned by `tm/verify/`, unchanged from v2.0.2
- **Pattern Library content**: v0.2 only defines the *kind*; the seed patterns (safety/liveness/fairness templates) are populated in Stage 5-3, not here
- **NL → Pattern AI pipeline**: owned by Stage 5-4, not here
- **Cross-pattern composition**: combining multiple patterns into joint properties — explicitly deferred to a later spec revision

### Principle

v0.2 is purely **additive**:

- One new value in `ArtifactType` enum: `property_pattern`
- One new artifact body schema: `schemas/v0/property_pattern.json`
- One new in-runtime AST schema: `PropertyPatternSpec` in `tm/artifacts/schema.py`
- Two new optional fields in `IntentBody` / `IntentSpec`
- No envelope structural changes; no canonicalization changes; no existing schema field changes

---

## 2. New artifact kind: `PropertyPattern`

A PropertyPattern is a **domain-neutral, reusable formal template** for expressing system properties. Its slots are filled at instantiation time (by IntentTree.slot_fills) to produce a concrete property statement that can be verified.

### Body schema (minimal v0.2 form)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `pattern_id` | string | yes | Stable identifier, e.g. `"safety.no_x_amplifies_y"` |
| `category` | enum: `safety` \| `liveness` \| `fairness` | yes | Which logical class this pattern belongs to |
| `title` | string | yes | Short human-readable label |
| `description` | string | no | Longer prose explanation of intent |
| `formula_template` | string | yes | CTL / LTL formula with `{slot_name}` placeholders. Domain-neutral. Example: `AG(¬controlled[{actor}].amplifies[{content}])` |
| `slots` | array of `Slot` | yes | Typed slot declarations; see below |
| `applicable_conditions` | array of string | no | Free-form prerequisites for when this pattern applies |
| `counterexamples` | array of `Counterexample` | no | Worked-out scenarios the pattern intends to prevent / require |
| `metadata` | object | no | Free-form tags, citations, attribution |

#### `Slot` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | Slot identifier matching `{name}` placeholder in `formula_template` |
| `type` | string | yes | Domain primitive reference (e.g. `"Actor"`, `"Content"`, `"Resource"`). Domain registries decide what types are legal |
| `description` | string | no | Free prose |
| `required` | boolean | no | Defaults to `true` if omitted |

#### `Counterexample` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `description` | string | yes | What goes wrong if pattern is violated |
| `scenario` | string | no | A worked-out trace, prose or pseudo-code |

### Envelope

PropertyPattern shares the same envelope as every other v0.x artifact (`artifact_id`, `status`, `artifact_type = "property_pattern"`, `version`, `created_by`, `created_at`, `body_hash`, `envelope_hash`, `meta`, `signature`). No new envelope fields.

### Governance lifecycle

PropertyPattern follows the standard Governance Baseline lifecycle:
1. AI / human drafts a candidate (`status: candidate`)
2. `tm artifacts verify` runs schema + lint (slot type integrity, applicable_conditions sanity)
3. Regression tests bound via `TestSuite` artifacts (Rule 3)
4. `tm artifacts accept` promotes to `status: accepted`, registers in `.tracemind/registry.jsonl`
5. Subsequent changes go through `tm patch propose` / `submit` / `approve` (Rule 2)

---

## 3. Additive extension to IntentBody / IntentSpec

Both file-level `schemas/v0/intent.json` and AST-level `_INTENT_SPEC_SCHEMA` add two optional fields:

| New field | Type | Required | Notes |
|-----------|------|----------|-------|
| `property_pattern_refs` | array of string | no | Pattern IDs this intent implements / is verified by. Defaults to empty `[]` |
| `slot_fills` | object | no | Map of `pattern_id → {slot_name → value}` providing per-pattern slot assignments. Defaults to empty `{}` |

### Reference semantics

- Each entry in `property_pattern_refs` MUST correspond to an accepted `PropertyPattern` artifact in the registry — **lint check** (not schema check, since cross-artifact existence cannot be expressed in JSON Schema)
- Each pattern referenced MUST have a matching key in `slot_fills` — **lint check**
- Each slot in `slot_fills[pattern_id]` MUST match the slot's declared `type` — **lint check**
- An IntentTree may reference **multiple** PropertyPatterns; they are conjunctive (the intent claims to satisfy all of them)

### v0.1 → v0.2 compatibility

A v0.1 IntentBody that omits both `property_pattern_refs` and `slot_fills` continues to be **schema-valid under v0.2**. The new fields are purely opt-in.

---

## 4. ArtifactType enum extension

`tm/artifacts/types.py`:

```python
class ArtifactType(str, Enum):
    INTENT = "intent"
    CAPABILITIES = "capabilities"
    PLAN = "plan"
    GAP_MAP = "gap_map"
    BACKLOG = "backlog"
    AGENT_BUNDLE = "agent_bundle"
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"
    PROPOSED_CHANGE_PLAN = "proposed_change_plan"
    EXECUTION_REPORT = "execution_report"
    PROPERTY_PATTERN = "property_pattern"   # NEW in v0.2
```

`schemas/v0/envelope.json` `artifact_type` enum is brought into sync with the full `ArtifactType` enum (the pre-v0.2 enum was 5 values, lagging behind the code). v0.2 envelope.json enum lists all 10 values. This sync is a **non-functional fix** — envelope.json was not loaded at runtime — but documents truth.

---

## 5. Canonicalization (unchanged)

All canonicalization rules from K-Ontology v0.1 §5 (lexicographic key ordering, set-like vs sequence-like list semantics, stable canonical JSON for hashing) apply unchanged to PropertyPattern bodies and to the new IntentBody fields.

Sequence-like fields in PropertyPattern: `slots`, `counterexamples`, `applicable_conditions` (order preserved).

Set-like fields in IntentBody extension: `property_pattern_refs` (deduplicated + lexicographically sorted before hashing).

---

## 6. Lint vs schema boundary (new rules)

| Concern | Mechanism |
|---------|-----------|
| PropertyPattern body structure (required fields, enum values, types) | **Schema** (`schemas/v0/property_pattern.json`) |
| Slot name matches `{name}` placeholder in `formula_template` | **Lint** (`tm/lint/property_pattern_lint.py`) |
| `category` value matches conventional naming of pattern_id prefix (e.g. `safety.*`) | **Lint** (advisory only, not blocking) |
| Referenced pattern_ids exist in registry | **Lint** |
| `slot_fills` keys cover all referenced patterns | **Lint** |
| `slot_fills[pattern_id]` keys cover all required slots | **Lint** |
| Slot value types match declared slot.type | **Lint** (deferred to Stage 5-3 once domain primitive registry is in place; v0.2 only checks names) |

---

## 7. Minimal example (canonical body)

```json
{
  "pattern_id": "safety.no_x_amplifies_y",
  "category": "safety",
  "title": "Controlled actor must not amplify target content",
  "description": "Across all reachable states, no actor under coordinated control amplifies the protected content set.",
  "formula_template": "AG(\u00ac controlled[{actor}].amplifies[{content}])",
  "slots": [
    {"name": "actor", "type": "Actor", "description": "Entity that may be under coordinated control", "required": true},
    {"name": "content", "type": "Content", "description": "Protected content reference", "required": true}
  ],
  "applicable_conditions": [
    "Target system exposes an actor identity registry",
    "Amplification events are observable in the trace"
  ],
  "counterexamples": [
    {
      "description": "A coordinated cluster sustains promotion of disallowed content over a 24h window",
      "scenario": "actor_cluster=A,B,C all amplify content=X at coordinated cadence"
    }
  ]
}
```

And an IntentBody that uses it:

```json
{
  "intent_id": "intent.protect_against_sybil_amplification",
  "title": "Prevent coordinated amplification of protected content",
  "context": "Anti-manipulation control loop must enforce no coordinated amplification.",
  "goal": "AG safety holds for actor and content slots",
  "property_pattern_refs": ["safety.no_x_amplifies_y"],
  "slot_fills": {
    "safety.no_x_amplifies_y": {
      "actor": "Actor:author_pub",
      "content": "Content:protected_set"
    }
  }
}
```

---

## 8. Compatibility & versioning

### From v0.1

- All v0.1 artifacts remain schema-valid under v0.2
- All v0.1 spec docs (`k-ontology-v0.1.md`, `intent-tree-v0.1.md`, `proposal-v0.1.md`, etc.) remain authoritative for their respective concerns
- No envelope structural changes; no `body_hash` recomputation required for existing accepted artifacts
- Regression test gate: `tests/test_v01_compat_under_v02.py` verifies every v0.1 fixture passes under v0.2 schemas

### To v0.3+

- New patterns can be added to the Pattern Library without bumping the ontology version
- Future revisions may add: pattern composition (intersection / disjunction), pattern parameterization across nested slots, evidence schema for pattern verification. These are deferred and will follow the same additive discipline.

---

## References

- [K-Ontology v0.1](k-ontology-v0.1.md) — base ontology spec, unchanged
- [Intent Tree v0.1](intent-tree-v0.1.md) — base intent spec, augmented additively here
- [Governance Baseline](../governance/baseline.md) — five non-negotiable rules; apply unchanged
- [Phase 5 plan](../../../.plan/phase-5-tracemind-generalization.md) — context for Stage 5-1
