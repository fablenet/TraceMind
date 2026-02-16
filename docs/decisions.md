# Decisions (决策/治理入口)

This page is the **constitution entry** for governance, gate pipeline, and change rules. It indexes existing docs and gives a minimal checklist.

---

## Governance baseline

- **Non-negotiable rules:** AI only produces candidates; every pass is an explicit artifact change; intents have regression tests; large splits use trace_links; no runtime self-authorization.
- Full text: [Governance Baseline](governance/baseline.md) and [CONTRIBUTING · Non-negotiable Governance Rules](CONTRIBUTING.md#non-negotiable-governance-rules).

---

## Gate pipeline (order)

Implementation: `tm/proposal/gate.py` (`run_proposal_gate`). Sequence:

1. **schema_validate** — Proposal and referenced patches/testsuites: canonical AST kind, envelope, schema.
2. **intents_validate** — Intent tree: ids, topology (single root, acyclic, parent exists), leaf success_criteria. Uses `tm.intent.tree_validator.validate_intent_tree`.
3. **proposal_lint_validate** — Proposal lint: refs exist, impacted_intents valid, scope/authorization checks.
4. **run_tests** — Run TestSuite(s) against policy (`tm tests run`). hard/compat failures fail the step.
5. **consistency_gate** — If registry provided: check consistency (e.g. C3); optional trace artifact.

Proposal gate CLI: `tm gate proposal --proposal <path> --intents <path> --policy <path>` (optional: `--registry`, `--json-report`, `--trace`).

---

## What can evolve / how

See [Regression rules](regression_rules.md).

- **hard / compat:** Old tests must pass. If not → only path is major (or schema) version bump + written rationale + new tests. No merge without fix or versioning.
- **evolving:** Additive change allowed; explain in PR. Breaking → same as hard/compat.
- **Major bump rule:** Intent schema v0→v1, artifact version increment, or spec version that signals breaking change; plus rationale and new tests.

---

## Key links

| Topic | Doc |
|-------|-----|
| Governance baseline | [governance/baseline.md](governance/baseline.md) |
| Regression / constraint levels | [regression_rules.md](regression_rules.md) |
| Proposal spec | [specs/proposal-v0.1.md](specs/proposal-v0.1.md) |
| CONTRIBUTING (gate, PR checklist) | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Key commands (copy-paste)

```bash
python -m tm gate proposal --proposal <path> --intents <path> --policy <path> [--registry <path>] [--json-report <path>]
python -m tm tests run --suite <path> --policy <path>
python -m tm intents validate --intents <path>
python -m tm intents coverage --intents <path> --tests <path> [--policy <path>]
```

---

## Manual checklist (decisions)

- [ ] All behavioral changes go through explicit artifacts (candidate/proposal/patch); no implicit prompt/code-only drift.
- [ ] Before merge: gate run (e.g. `tm gate proposal ...`) or equivalent CI; gate is mandatory for proposal-driven changes.
- [ ] hard/compat failures: either fixed or version bumped + rationale + new tests; no merge without one of these.
- [ ] Evolving warnings: explained in PR or handled via version bump path.
- [ ] No runtime self-authorization; resource effects go through PolicyGuard or approval token.
- [ ] Governance baseline and regression rules are followed; links above are the source of truth.
