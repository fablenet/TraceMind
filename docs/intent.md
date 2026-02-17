# Intent (intent 入口)

This page is the **constitution entry** for intent tree, traceability, leaf requirements, and coverage gate. It indexes the spec and implementation; full rules are in the linked docs.

---

## Intent tree structure

- **Authority for parent-child:** Parent/related are **always under a trace_links object**, not top-level. Implementation resolves: `trace_links`, then `metadata.trace_links`, then `spec.trace_links` (see `tm/intent/tree_validator.py`). Canonical paths: **`trace_links.parent_intent`** / **`trace_links.related_intents`**, or **`metadata.trace_links.parent_intent`** / **`metadata.trace_links.related_intents`**, or **`spec.trace_links.*`**. **Do not** use `parent_intent` / `related_intents` as top-level fields. Spec: [Intent tree v0.1](specs/intent-tree-v0.1.md); envelope: [K-Ontology](specs/k-ontology-v0.1.md).
- **Node minimum fields:** `id`, `title` or `summary`; a **trace_links** object (optional) at one of: node root, `metadata.trace_links`, or `spec.trace_links`. Inside that object: `parent_intent` (string), `related_intents` (array of string, set-like).
- **Single root, acyclic, parent exists:** At most one root (no parent); following the chosen parent field (e.g. `trace_links.parent_intent`) must not form a cycle; every non-root’s parent ID must exist in the tree.
- **Leaf:** Node that no other node has as its parent (i.e. no node's `trace_links.parent_intent` or `metadata.trace_links.parent_intent` points to this node's `id`). **Leaf must have** `success_criteria` (minimal structure) and **tests coverage** (validated by `tm intents coverage` or M5.2 tooling).

---

## Traceability and coverage

- **Traceability:** Large intent splits declare parent/related under a trace_links object: `trace_links.parent_intent` / `trace_links.related_intents` or `metadata.trace_links.parent_intent` / `metadata.trace_links.related_intents`; tools verify topology (see [Governance Baseline](governance/baseline.md) Rule 4).
- **Coverage gate:** Leaf intents must be covered by tests (TestSuite `intent_refs` or equivalent). Run `tm intents coverage --intents <path> --tests <path>`; uncovered leaves fail the gate.

---

## Key links

| Topic | Doc |
|-------|-----|
| Intent tree spec | [specs/intent-tree-v0.1.md](specs/intent-tree-v0.1.md) |
| Governance Rule 4 | [governance/baseline.md](governance/baseline.md) |
| Regression / coverage | [regression_rules.md](regression_rules.md) |

---

## Key commands (copy-paste)

```bash
# Validate intent tree (ids, topology, leaf success_criteria)
python -m tm intents validate --intents <path> [--json]

# Coverage: leaf intents vs TestSuite intent_refs (and optional policy rule intent_refs)
python -m tm intents coverage --intents <path> --tests <path> [--policy <path>]
```

Example:

```bash
python -m tm intents validate --intents specs/examples/intents/tree.json
python -m tm intents coverage --intents specs/examples/intents/tree.json --tests tests/fixtures/policy/v0.1 --policy policies/default.yaml
```

---

## Manual checklist (intent)

- [ ] Intent tree has a single root; no cycles; every non-root's parent reference (e.g. `trace_links.parent_intent` or `metadata.trace_links.parent_intent`) exists in the tree.
- [ ] Parent/related are under a trace_links object only (trace_links / metadata.trace_links / spec.trace_links); **not** top-level `parent_intent` or `related_intents` on the intent node.
- [ ] Parent-child authority is unique (one of `trace_links`, `metadata.trace_links`, or `spec.parent_id` for the whole tree, not mixed).
- [ ] Every leaf node has `success_criteria` present.
- [ ] Every leaf is covered by tests (e.g. TestSuite intent_refs); `tm intents coverage` run confirms no uncovered leaves (or explains exception).
- [ ] Large intent splits declare parent/related inside trace_links (e.g. `trace_links.parent_intent` and `trace_links.related_intents`, or `metadata.trace_links.*`); intent tree file passes `tm intents validate` before merge.
