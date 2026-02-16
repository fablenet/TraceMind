# Regression rules (硬约束 #3)

This document defines **regression constraint levels** and the **change advancement rules** that implement hard constraint #3: *each intent/spec must bind to regression tests; new specs must pass old tests, or version bump + explanation + new tests*.

See [Governance Baseline](governance/baseline.md) for the full set of non-negotiable rules.

---

## 1. Constraint levels: hard / compat / evolving

| Level | Meaning | When tests fail |
|-------|--------|-----------------|
| **hard** | Invariant. No behavioral change allowed without an explicit version/contract change. Old tests must pass on the old contract. | Failing old tests is **forbidden** unless you follow the only allowed path below. |
| **compat** | Backward compatibility. New behavior must not break existing consumers; additive changes only within the same major/schema version. | Same as hard: old tests must pass; otherwise version bump + rationale + new tests. |
| **evolving** | Additive or versioned evolution. New behavior may extend the contract; old tests still apply to the old surface. New tests cover the new surface. | Old tests must pass. New behavior requires new tests; breaking changes require major bump + rationale + new tests. |

In practice for TraceMind:

- **hard**: Policy determinism (see [policy_determinism.md](policy_determinism.md)), artifact schema contracts, verification invariants. No silent drift.
- **compat**: API/CLI flags, artifact envelope fields that consumers depend on. Additive-only within a major version.
- **evolving**: New intents, new schema versions, new steps. Old tests remain; new behavior is covered by new tests and, if breaking, by a version bump and written rationale.

---

## 2. Change advancement rules

1. **Old tests must pass.** Any change (code, schema, intent, policy) must not cause existing regression tests to fail without following the exception path below.

2. **If old tests do not pass**, the **only** allowed path is:
   - **(1) Major (or schema) version bump** — e.g. intent schema v0 → v1, or artifact/spec version increment that signals a breaking change.
   - **(2) Written rationale** — in PR description, commit message, or a designated doc (e.g. CHANGELOG, compatibility matrix) explaining why the break is intentional and what replaces the old behavior.
   - **(3) New tests added** — new tests that cover the new behavior and, where applicable, the new contract so that future changes cannot silently regress again.

3. **You may not** delete or relax existing tests solely to make a new spec pass. Regressions are forbidden unless explicitly versioned and documented (ConsistencyReport C3 alignment).

---

## 3. How to run regression and gate

### Running tests

- **Full test suite (regression):**
  ```bash
  pytest -q tests/
  ```
  Or, when available:
  ```bash
  tm tests run
  ```
  Use this before every PR; CI runs the same.

- **Gate run (regression + consistency / golden checks):**
  When the gate CLI is available:
  ```bash
  tm gate run --report report.json
  ```
  This runs the regression suite and any artifact/consistency gates, writing a structured **report** (e.g. `report.json`) with pass/fail and, on failure, which constraints failed.

- **Current CI equivalent:**  
  CI runs `pytest -q`, `tm artifacts verify` on golden artifacts, and workflow-specific steps (e.g. agent bundle smoke). The gate report is the aggregate of these outcomes; a single `report.json` can be produced by a dedicated script or future `tm gate run` that runs the same checks and writes the result.

### Using the report

- **report.json** (or equivalent) should include at least:
  - Overall pass/fail.
  - Per-check results (e.g. pytest exit code, artifact verify results, golden diff).
  - For failures: which constraint level (hard/compat/evolving) was violated and where (test name, artifact, rule).

This supports auditing and ensures that any failure can be traced to a specific regression rule and the allowed path (version bump + rationale + new tests) when old tests no longer pass.
