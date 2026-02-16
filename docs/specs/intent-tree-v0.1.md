# Intent tree v0.1

This spec defines the **intent tree** structure: minimum node fields, the single authority for parent-child links, topology rules, leaf intent requirements, and a minimal example.

---

## 1. Intent node minimum fields

Each intent node in the tree MUST have at least:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier for this intent (e.g. `TM-INT-0001`). |
| `title` or `summary` | string | yes | Short human-readable label; at least one of `title` or `summary` must be present. |
| `trace_links` | object | no | Parent and related intent references; see below. |

`trace_links` when present MUST support:

| Subfield | Type | Description |
|----------|------|-------------|
| `parent_intent` | string | ID of the parent intent (this node is a child of that intent). |
| `related_intents` | array of string | IDs of sibling or related intents (set-like; no duplicate IDs). |

Other fields (e.g. `spec`, `metadata`, `success_criteria`, `tests`) are defined elsewhere or in extension; this spec only mandates the minimum above.

---

## 2. Single authority for parent-child

The **only** place that defines the parent-child relationship MUST be one of:

- **Option A**: `trace_links.parent_intent` on each intent node (recommended; aligns with [Governance Baseline](governance/baseline.md) Rule 4).
- **Option B**: `spec.parent_id` (or equivalent) on each intent node.

**You must pick one** for the whole tree. Do not mix: e.g. do not use both `trace_links.parent_intent` and `spec.parent_id` as sources of truth. Tools and M5.2 validation assume a single authority; the spec recommends **`trace_links.parent_intent`** (see [Governance Baseline](../governance/baseline.md) Rule 4).

---

## 3. Topology rules

- **Acyclicity**: Following `parent_intent` (or the chosen parent field) from any node must never form a cycle. The graph must be a DAG (directed acyclic graph) with edges from child → parent.
- **Parent must exist**: For every non-root intent, the referenced parent ID MUST exist as another node in the tree (or in the declared intent set). Root intents have no parent (omit `parent_intent` or set it to null/absent).
- **related_intents**: Multiple related intents are allowed. They MUST NOT introduce a parent cycle: i.e. following only `parent_intent` edges must remain acyclic. `related_intents` are for sibling/cross-reference only; they do not define parent-child. Tools may validate that no `related_intents` entry is an ancestor via `parent_intent` if that would create ambiguity.

---

## 4. Leaf intent definition

A **leaf intent** is an intent node that has **no children**: no other node in the tree declares this node’s `id` as its `parent_intent` (or chosen parent field).

Root intents (no parent) may or may not be leaves; a root is a leaf if no node points to it as parent.

---

## 5. Leaf intent requirements

Every **leaf intent** MUST have:

1. **success_criteria** — Minimal structure is sufficient (e.g. an object or array of criteria). The exact schema can be defined elsewhere; this spec only requires the field to be present so that leaf outcomes are verifiable.
2. **Tests coverage** — Leaf intents MUST be covered by tests (e.g. regression tests, policy test suite, or intent-specific tests). Coverage is validated by the **M5.2** tooling; the intent tree and test suite together must satisfy the coverage rules (e.g. every leaf referenced by at least one test or rule).

Non-leaf intents may omit `success_criteria` and tests if they are only used for grouping or decomposition; leaves are the units of verification.

---

## 6. Minimal intent tree JSON example

Example with parent and related links (authority = `trace_links.parent_intent`):

```json
{
  "intents": [
    {
      "id": "TM-INT-ROOT",
      "title": "Root capability",
      "trace_links": {}
    },
    {
      "id": "TM-INT-A",
      "title": "Sub-intent A",
      "trace_links": {
        "parent_intent": "TM-INT-ROOT",
        "related_intents": ["TM-INT-B"]
      }
    },
    {
      "id": "TM-INT-B",
      "title": "Sub-intent B",
      "trace_links": {
        "parent_intent": "TM-INT-ROOT",
        "related_intents": ["TM-INT-A"]
      }
    },
    {
      "id": "TM-INT-A1",
      "summary": "Leaf under A",
      "trace_links": {
        "parent_intent": "TM-INT-A",
        "related_intents": []
      },
      "success_criteria": { "type": "minimal" },
      "tests": []
    }
  ]
}
```

- **TM-INT-ROOT**: root (no parent); not a leaf (A and B point to it).
- **TM-INT-A**, **TM-INT-B**: children of root; siblings via `related_intents`; not leaves (A1 points to A).
- **TM-INT-A1**: child of A; leaf (no node has `parent_intent: "TM-INT-A1"`). Has `success_criteria` and `tests` (empty array; M5.2 would require coverage to be filled).
