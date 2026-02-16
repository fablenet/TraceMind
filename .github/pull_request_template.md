## Description

<!-- Brief description of the change -->

## Type of Change

- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change (requires version bump + rationale + new tests)
- [ ] Documentation update
- [ ] Proposal-driven change (intent/policy/workflow)

## Proposal-driven Changes (if applicable)

See [CONTRIBUTING · Proposal-driven Workflow](docs/CONTRIBUTING.md#proposal-driven-workflow-m61) for full checklist.

- [ ] **Proposal created** — `proposals/<proposal-id>.json` (from `templates/proposal_v0.1.json`)
- [ ] **Gate passed** — `tm gate proposal proposals/<proposal-id>.json` returns pass (or evolving warnings explained in PR)
- [ ] **Patch included** — Patch artifacts referenced in proposal exist and are valid
- [ ] **Tests included** — Test suites referenced in proposal exist and cover the change
- [ ] **Impact declared** — `impacted_intents` lists all affected intent IDs
- [ ] **Risk assessed** — `risk` is `low` | `medium` | `high`
- [ ] **Rationale provided** — `summary` and optionally `rationale` explain the change
- [ ] **Failure handling** — Hard/compat failures: fixed or version bumped + rationale + new tests. Evolving warns: explained in PR.

## Governance Checklist

- [ ] **Rule 1** — AI outputs are candidates only; they pass `tm artifacts verify` and regression tests before being accepted or patched.
- [ ] **Rule 2** — Every behavioral change is an explicit artifact/patch change (no implicit prompt tweaks or code branches).
- [ ] **Rule 3** — Each intent/spec has regression tests; new specs pass old tests, or version bump + explanation + new tests added.
- [ ] **Rule 4** — Large intent splits declare `trace_links.parent_intent` / `trace_links.related_intents`; tools can verify traceability.
- [ ] **Rule 5** — No runtime self-authorization; all resource effects go through PolicyGuard or approval token.

## Gate Results

<!-- If gate reported failures, explain how they were handled: -->
- [ ] Gate passed (no failures)
- [ ] Hard/compat failures: Fixed or version bumped + rationale + new tests added
- [ ] Evolving warnings: Explained in PR description

## Testing

- [ ] Regression tests pass: `pytest -q tests/`
- [ ] New tests added for new behavior
- [ ] CI passes

## Related Issues

<!-- Link to related issues or proposals -->
