# TraceMind K-Ontology & Canonical AST v0.1

**Version**: tracemind.io/v0.1  
**Status**: Specification (M1.1)  
**Scope**: Ontology boundaries and canonical AST envelope; no rendering syntax; no implementation.

---

## 1. Scope & Non-Goals

### In scope (v0.1)
- Definition of the **three-layer ontology** (data / meta / meta-meta) and their boundaries
- Definition of the **canonical AST** as the single source of truth for gate, diff, regression, and consistency checks
- **Envelope structure** and **AST type catalog** sufficient for M1.2 (JSON Schema) and M1.3 (fmt)
- Canonicalization rules for stable printing and hashing

### Out of scope (v0.1)
- **Rendering syntax** (YAML, SML, k8s-like): treated as parse/print only, not part of the ontology
- **Complex expression language**: nested expressions in `when`/`then` are deferred to M2/M3
- Implementation code (Python or otherwise)
- Specific rendering formats or file layouts

### Principle
Gate, diff, regression, and consistency tooling operate on **canonical AST only**. Representations (YAML/SML/etc.) are produced and consumed via parse/print; the ontology lives in the canonical AST.

---

## 2. Three-Layer Ontology

### 2.1 Data layer (值域)
Values and observations that systems produce or consume. Roles:
- **Observation**: A single data point or event (e.g. sensor reading, log entry)
- **Trace**: A sequence of observations over time (e.g. execution trace, request trace)
- **Metric**: Aggregate or derived measure (e.g. latency p99, throughput)

For v0.1 these are **shells** only: sufficient to name and reference, not to fully type.

### 2.2 Meta layer (Control — 关于 data 的规则)
Rules and specifications that govern how data is interpreted, constrained, or processed.
- **Intent**: Declared goal, constraints, and success criteria for a system or subsystem
- **Policy**: Set of constraints and invariants over state/behavior
- **Workflow**: Ordered steps and transitions that realize an intent under a policy
- **Guard**: Gate that allows or denies effects based on policy evaluation

### 2.3 Meta-meta layer (AI — 关于 meta 的规则)
Artifacts and reports that govern the **evolution** and **gating** of meta-layer objects.
- **Candidate**: Proposed artifact (intent/policy/workflow/etc.) before verification
- **Proposal**: Structured change request (patch proposal) with rationale and approvals
- **Patch**: Applied changes to a meta artifact
- **TestSuite**: Collection of regression tests bound to intents
- **RegistryEntry**: Record of an accepted artifact in the registry
- **ConsistencyReport**: Result of consistency checks (C1/C2/C3, etc.) over meta artifacts

---

## 3. Canonical AST Envelope (common fields)

All AST resources share a common envelope. This is **ontology**, not YAML: the canonical form is JSON.

### Top-level structure

| Field      | Type   | Required | Notes |
|-----------|--------|----------|-------|
| `api_version` | string | yes | Fixed `"tracemind.io/v0.1"` |
| `kind`    | string | yes | Enum; see §4 |
| `metadata`| object | yes | See below |
| `spec`    | object | yes | Kind-specific payload; structure depends on `kind` |

### `metadata` object

| Field        | Type   | Required | Notes |
|--------------|--------|----------|-------|
| `id`         | string | yes | Stable identifier (e.g. `artifact_id`, `proposal_id`) |
| `version`    | string | yes | Semantic version or version tag (e.g. `"1.0.0"`, `"v0"`) |
| `trace_links`| object | no  | See below |
| `labels`     | object | no  | `map<string, string>` for tagging |

### `trace_links` object

| Field             | Type     | Required | Notes |
|-------------------|----------|----------|-------|
| `parent_intent`   | string   | no       | Parent intent ID when this resource is derived from a larger intent |
| `related_intents` | [string] | no       | Sibling or related intent IDs for traceability |

### Field semantics
- **Must exist**: `api_version`, `kind`, `metadata`, `metadata.id`, `metadata.version`, `spec`
- **Optional**: `metadata.trace_links`, `metadata.labels`, and kind-specific `spec` fields
- **Forbidden unknown fields**: The envelope and `metadata` follow closed schema intent (`additionalProperties: false`): unknown keys at envelope root or under `metadata` are invalid. Kind-specific `spec` schemas define their own open/closed behavior.

---

## 4. AST Type Catalog (v0.1 minimal)

### 4.1 Control / Meta kinds

#### IntentTree
Intent specification with goal, constraints, and traceability.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `intent_id` | string | yes | Unique intent identifier |
| `title` | string | yes | Short summary |
| `goal` | string | yes | Measurable outcome |
| `context` | string | no | Why this matters |
| `trace_links` | object | no | `parent_intent`, `related_intents` (set-like) |

`metadata.trace_links` and `spec.trace_links` both support `parent_intent` / `related_intents` for topology and coverage checks (Rule 4).

#### PolicySet
Collection of policy rules.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `policy_id` | string | yes | Unique policy identifier |
| `rules` | array | yes | List of rule objects (min 1) |

Each rule (minimal v0.1 structure):
- `when`: object with simple conditions (no nested expressions); e.g. `{"effect": "write", "target": "state:workload"}`
- `then`: object with action; e.g. `{"allow": true}` or `{"allow": false, "reason": "..."}`

#### Workflow
Ordered steps and transitions realizing an intent.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `workflow_id` | string | yes | Unique workflow identifier |
| `intent_id` | string | yes | Intent this workflow realizes |
| `policy_id` | string | yes | Policy governing execution |
| `steps` | array | yes | Ordered sequence (sequence-like) |
| `transitions` | array | no | State transitions |

#### GuardRule
Standalone guard gate.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `guard_id` | string | yes | Unique guard identifier |
| `scope` | string | yes | Scope of evaluation |
| `required_for` | string or [string] | no | Effects/operations this guard applies to |

### 4.2 Meta-meta / AI kinds

These kinds must be usable for **gate** operations: validate, consistency, regression.

#### Candidate
Proposed artifact before verification (Rule 1: AI only produces candidates).

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `artifact_type` | string | yes | One of intent, policy, workflow, guard, etc. |
| `status` | string | yes | `"candidate"` |
| `spec` | object | yes | The proposed meta payload (IntentTree, PolicySet, etc.) |
| `intent_refs` | [string] | no | Intent IDs this candidate references (set-like) |

#### Proposal
Patch proposal (draft/submitted/approved).

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `proposal_id` | string | yes | Stable proposal ID |
| `target` | string | yes | `policy` \| `intent` \| `workflow` \| `config` |
| `target_ref` | string | yes | Path or ID of target artifact |
| `patch_kind` | string | no | e.g. `tighten_guard` |
| `rationale` | string | yes | Why this change |
| `expected_effect` | string | yes | Expected outcome |
| `changes` | array | yes | Ordered list of patch ops (sequence-like) |
| `status` | string | yes | `DRAFT` \| `SUBMITTED` \| `APPROVED` |
| `impacted_intents` | [string] | no | Intent IDs affected (set-like) |

#### Patch
Applied change set.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `proposal_id` | string | yes | Source proposal |
| `target_ref` | string | yes | Target artifact path/ID |
| `changes` | array | yes | Ordered ops (sequence-like) |
| `applied_at` | string | yes | RFC 3339 timestamp |

#### TestSuite
Regression tests bound to intents (Rule 3).

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `suite_id` | string | yes | Unique suite identifier |
| `intent_refs` | [string] | yes | Intent IDs this suite covers (set-like) |
| `tests` | array | yes | Ordered test cases (sequence-like) |
| `stability` | string | no | `stable` \| `unstable` for golden expectations |

#### RegistryEntry
Accepted artifact record in registry.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `artifact_id` | string | yes | Artifact identifier |
| `artifact_type` | string | yes | intent, policy, workflow, etc. |
| `body_hash` | string | yes | Hash of canonical body |
| `path` | string | yes | Path to artifact file |
| `intent_id` | string | no | For intent-type artifacts |
| `version` | string | yes | Schema/artifact version |
| `created_at` | string | yes | RFC 3339 |
| `status` | string | yes | `accepted` |
| `meta` | object | no | `invariant_status`, `derived_from`, etc. |

#### ConsistencyReport
Output of consistency check (C1/C2/C3).

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `artifact_id` | string | yes | Artifact checked |
| `issues` | array | yes | List of issues (sequence-like) |

Each issue: `code` (C1/C2/C3), `severity`, `summary`, `details`.

---

## 5. Canonicalization Rules (M1 core)

Canonical AST must be **stable** for diff and hash. The following rules apply before any comparison or hashing.

### 5.1 Map key ordering
- All object keys sorted in **lexicographic (dictionary) order** before serialization.

### 5.2 List semantics

**Set-like fields** (must be deduplicated and sorted):
- `metadata.trace_links.related_intents`
- `spec.trace_links.related_intents` (when present in IntentTree)
- `intent_refs` (in Candidate, TestSuite)
- `impacted_intents` (in Proposal)
- Other fields explicitly documented as "set-like" in the type catalog

**Sequence-like fields** (order preserved):
- `spec.steps` (Workflow)
- `spec.rules` (PolicySet) — rule order may matter for evaluation
- `spec.changes` (Proposal, Patch)
- `spec.tests` (TestSuite)
- `issues` (ConsistencyReport)
- Any array not listed as set-like

### 5.3 Stable printing
- **Canonical JSON** is the normative format for diff and hash.
- No trailing commas, no comments, UTF-8 encoding.
- Floating-point numbers: use a deterministic representation (e.g. shortest decimal that round-trips).
- Strings: no escape variations (e.g. prefer `\n` over literal newline where schema permits).

### 5.4 Hashing note
- Hash is computed over the **canonical print** of the AST (or of the `spec` body for body_hash).
- Algorithm: SHA-256 (or as specified in registry/verifier). Implementation deferred to M1.3; this spec defines the **input** to the hash (canonical JSON string).

---

## 6. Validation vs Lint Boundaries

| Concern | Mechanism | Notes |
|---------|-----------|-------|
| Structure, types, required fields, enums | **Schema (JSON Schema)** | `additionalProperties: false` where appropriate; schema rejects unknown envelope keys |
| Unknown `metadata` keys | **Schema** | Envelope `metadata` closed |
| Unknown `spec` keys (per kind) | **Schema** | Per-kind `spec` schema defines closed/open |
| Cross-resource references (e.g. `intent_id` exists) | **Lint** | Cannot be expressed purely in JSON Schema |
| Acyclicity of `parent_intent` / `related_intents` | **Lint** | Topology check |
| Coverage (e.g. all intents have TestSuite) | **Lint** | Cross-artifact check |
| `allowed_changes` / policy allowlist scope | **Lint** | Semantic check |
| C1/C2/C3 regression (invariant_status, body_hash) | **Lint** | Consistency checker |
| RFC 3339 timestamp format | **Schema** | `format: date-time` |

Schema handles structure and basic semantics; lint handles cross-resource and semantic rules.

---

## 7. Examples (canonical AST JSON)

All examples use `snake_case` consistently. These are **ontology JSON**, not YAML.

### 7.1 IntentTree (with trace_links)

```json
{
  "api_version": "tracemind.io/v0.1",
  "kind": "IntentTree",
  "metadata": {
    "id": "intent-tm-0002",
    "version": "1.0.0",
    "trace_links": {
      "parent_intent": "TM-INT-0000",
      "related_intents": ["TM-INT-0001", "TM-INT-0003"]
    }
  },
  "spec": {
    "intent_id": "TM-INT-0002",
    "title": "Child intent: notification latency",
    "goal": "Improve notification latency by 30%",
    "context": "Sub-goal of parent TM-INT-0000",
    "trace_links": {
      "parent_intent": "TM-INT-0000",
      "related_intents": ["TM-INT-0001", "TM-INT-0003"]
    }
  }
}
```

### 7.2 PolicySet (1 policy rule, minimal when/then)

```json
{
  "api_version": "tracemind.io/v0.1",
  "kind": "PolicySet",
  "metadata": {
    "id": "policy-maint-default",
    "version": "1.0.0"
  },
  "spec": {
    "policy_id": "maint.default",
    "rules": [
      {
        "when": {
          "effect": "write",
          "target": "state:workload"
        },
        "then": {
          "allow": true
        }
      }
    ]
  }
}
```

### 7.3 Proposal + Patch + TestSuite (minimal closed loop)

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
    "proposal_id": "prop-001",
    "target": "policy",
    "target_ref": "policies/maint.json",
    "patch_kind": "tighten_guard",
    "rationale": "Prevent unauthorized state writes",
    "expected_effect": "Block writes without approval token",
    "changes": [
      {
        "path": "/spec/rules/0/then/allow",
        "op": "set",
        "value": false
      }
    ],
    "status": "APPROVED",
    "impacted_intents": ["TM-INT-0001"]
  }
}
```

```json
{
  "api_version": "tracemind.io/v0.1",
  "kind": "Patch",
  "metadata": {
    "id": "patch-prop-001",
    "version": "1.0.0"
  },
  "spec": {
    "proposal_id": "prop-001",
    "target_ref": "policies/maint.json",
    "changes": [
      {
        "path": "/spec/rules/0/then/allow",
        "op": "set",
        "value": false
      }
    ],
    "applied_at": "2025-02-15T12:00:00Z"
  }
}
```

```json
{
  "api_version": "tracemind.io/v0.1",
  "kind": "TestSuite",
  "metadata": {
    "id": "suite-tm-0001",
    "version": "1.0.0"
  },
  "spec": {
    "suite_id": "suite-tm-0001",
    "intent_refs": ["TM-INT-0001"],
    "stability": "stable",
    "tests": [
      {
        "id": "test-001",
        "description": "Verify policy blocks write without token"
      }
    ]
  }
}
```

---

## 8. Compatibility & Versioning

### v0.1 upgrade strategy
- **Major bump**: Breaking changes to envelope, kind set, or canonicalization rules require `api_version` bump (e.g. `tracemind.io/v0.2`).
- **Migration note**: When bumping major:
  - Document migration path from v0.1 to new version
  - Provide tooling or guidance to convert v0.1 AST to new format where possible
  - Deprecated fields may be supported for one major cycle with a migration warning

### Additive changes (minor)
- New optional fields in `metadata` or kind `spec` are permitted without major bump.
- New enum values for `kind` or status fields may be added in minor releases.
- Consumers must ignore unknown fields in additive regions.

### Semantic versioning
- `metadata.version` follows semver where applicable (`major.minor.patch`).
- `api_version` encodes the ontology/schema version; it is not semver for the application.

---

## References
- [Governance Baseline](../governance/baseline.md) — Five non-negotiable rules
- [CONTRIBUTING — Non-negotiable Governance Rules](../CONTRIBUTING.md#non-negotiable-governance-rules)
- [Flow IR v0.1](../ir/v0.1/spec.md) — Compile-time IR (separate from ontology)
