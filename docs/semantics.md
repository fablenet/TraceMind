# Semantics (语义入口)

Constitution entry for canonical semantics and deterministic output. Indexes specs; does not replace them.

---

## Canonical AST and rendering

- **Canonical AST is the ontology.** Gate, diff, regression, consistency use canonical AST (JSON). See [K-Ontology & Canonical AST v0.1](specs/k-ontology-v0.1.md).
- **Rendering is parse/print only.** YAML/JSON/SML are representations. No semantics from file format.
- **Stable diff/hash:** `canonical_json_bytes` in `tm.policy.deterministic`: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

---

## Deterministic policy (M2)

Source: `tm/policy/deterministic.py` and [Policy determinism (M2)](policy_determinism.md).

- **Rule order:** priority DESC then id ASC. Conflict: same actuator, multiple set → **first-wins**; later entries **applied: false**.
- **Condition AST:** all/any/not; comparisons; var path; missing var → None; type mismatch → False.
- **Canonical action_log:** sort_keys, fixed separators; no non-deterministic fields.

---

## Tests gate: hard / compat / evolving (v0.1)

See [Regression rules](regression_rules.md). **hard/compat:** fail → exit 1; fix or major bump + rationale + new tests. **evolving:** additive OK; breaking → same as hard.

---

## Key links

| Topic | Doc |
|-------|-----|
| Ontology | [specs/k-ontology-v0.1.md](specs/k-ontology-v0.1.md) |
| Policy determinism | [policy_determinism.md](policy_determinism.md) |
| Trace format | [specs/trace-format-v0.1.md](specs/trace-format-v0.1.md) |
| Regression | [regression_rules.md](regression_rules.md) |

---

## Key commands

```bash
python -m tm validate --help
python -m tm fmt --help
python -m tm tests run --suite <path> --policy <path> --help
python -m tm replay diff --help
```

---

## Manual checklist (semantics)

- [ ] Action log is canonical (sort_keys, separators; no non-deterministic fields).
- [ ] Rule order: priority DESC, id ASC; conflict first-wins; applied: false for later same-actuator set.
- [ ] Condition AST: missing var → None; type mismatch → False.
- [ ] Diff/hash use canonical_json_bytes (or equivalent).
- [ ] Test gate: hard/compat fail → block; evolving → explain or version path.
- [ ] Semantics from AST only; rendering is parse/print only.
