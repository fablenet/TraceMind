# Contributing

## Dev setup
- Python 3.11+
- Ensure repo root is on `PYTHONPATH` (pytest.ini or env var)

### Reproducible local venv

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
python -m pip install -r requirements-dev.txt
pytest -q
```

### CLI smoke checks (no runtime execution)

```bash
python -m tm fmt --help
python -m tm validate --help
```

## Branching
- Feature branches like `feat/<area>-<slug>`

## Commits
- Conventional Commits:
  - `feat(ai): add llm client`
  - `feat(steps): add ai.llm_call`
  - `docs: add policy adapter how-to`

## Tests
- Add tests under `tests/` for new modules; avoid modifying legacy tests.

## Non-negotiable Governance Rules

These rules apply to all contributions involving intents, artifacts, AI outputs, or policy/workflow changes. See [Governance Baseline](governance/baseline.md) for full details.

### Candidate vs Accepted

- **Candidate** (`status: candidate`): An artifact produced by AI or humans before verification. It has no `body_hash`/`envelope_hash` until the verifier computes them.
- **Accepted** (`status: accepted`): A candidate that has passed schema validation, `tm artifacts verify`, and regression tests. Only accepted artifacts may enter the registry or patch pipeline.

### Required Commands (copy-paste)

```bash
# 1. Validate flows/policies for conflicts (if changing flows or policies)
tm validate --flows flows/*.yaml --policies policies/*.json

# 2. Validate artifacts for schema conformance
tm validate specs/examples/artifacts_v0/*.yaml

# 3. Verify a candidate artifact (returns accepted or rejected)
tm artifacts verify path/to/candidate.yaml

# 4. Run regression tests
pytest -q tests/

# 5. Accept a verified candidate into registry
tm artifacts accept path/to/candidate.yaml --out .tracemind/artifacts --registry .tracemind/registry.jsonl
```

### When Regression Tests Fail

- **New spec must pass old tests.** If your change causes existing tests to fail, you may not simply delete or relax tests.
- **Allowed path**: (1) version bump (e.g., intent schema v0 → v1 or artifact version increment), (2) written explanation in PR/commit, (3) new tests added to cover the new behavior. Align with ConsistencyReport C3 semantics: regressions are forbidden unless explicitly versioned and documented.

## Proposal-driven Workflow (M6.1+)

For changes that modify intents, policies, workflows, or other meta-layer artifacts, use the **proposal-driven workflow**.

### AI output boundaries

- **AI 只产出 candidate / proposal / patch** — AI only produces `candidate`, `proposal`, or `patch` artifacts. AI never directly writes `status: accepted` or executes changes.
- **All AI outputs are candidates** until they pass verification and gate checks.

### 本地一键命令 (One-command gate check)

Before submitting a PR, run the gate locally:

```bash
# Gate a proposal (validates schema, lint, references, authorization)
tm gate proposal path/to/proposal.json

# Or gate a patch directly
tm gate patch path/to/patch.yaml --intents path/to/intents.json
```

The gate returns:
- **Pass**: Proposal is valid and ready for review.
- **Fail**: See failure handling below.

### 失败处理 (Gate failure handling)

Gate failures are classified by constraint level (see [Regression rules](regression_rules.md)):

| Level | Action Required |
|-------|----------------|
| **hard** / **compat** | **必须修复**或走唯一允许路径：major/schema version bump + written rationale + 新测试补齐。不修复或未做版本升级则不得合并。 |
| **evolving** | **Warning** — 需在 PR 中解释变更为何是预期内的、如何扩展（而非破坏）契约；若为破坏性变更，则须走 version bump + rationale + 新测试。 |

Summary:
- **hard / compat**: Must fix, or major bump + rationale + new tests. No merge without one of these.
- **evolving warn**: Must explain in PR; if breaking, follow version bump + rationale + new tests.

Examples:

- **hard failure** (e.g. policy determinism violation): Fix the proposal/patch to maintain determinism, or bump the schema version and document the intentional change.
- **compat failure** (e.g. breaking API contract): Either make the change backward-compatible, or bump major version + rationale + new tests.
- **evolving warning** (e.g. new intent added): Explain in PR that this is additive and does not break existing intents.

### Templates

Minimal v0.1 templates (canonical AST, snake_case) are in the repo root:

| Template | Path | Use |
|----------|------|-----|
| Proposal | `templates/proposal_v0.1.json` | Copy to `proposals/<id>.json`, fill `impacted_intents`, `patch_refs`, `testsuite_refs`, `risk`, `summary`, `rationale`. |
| Patch | `templates/patch_v0.1.json` | Copy to `patches/<id>.json` or `.yaml`; fill `target`, `target_ref`, `operations`. |
| TestSuite | `templates/tests_v0.1.json` | Copy to tests or reference from proposal `testsuite_refs`; fill `spec.tests`. |

See [Proposal v0.1 spec](specs/proposal-v0.1.md) for field semantics.

### Proposal workflow steps

1. **Create proposal** (from template):
   ```bash
   cp templates/proposal_v0.1.json proposals/my-proposal.json
   # Edit proposals/my-proposal.json (impacted_intents, patch_refs, testsuite_refs, risk, summary)
   ```

2. **Create patch** (if needed):
   ```bash
   cp templates/patch_v0.1.json patches/my-patch.json
   # Edit patches/my-patch.json (target, target_ref, operations)
   ```

3. **Create/update tests**:
   ```bash
   cp templates/tests_v0.1.json tests/fixtures/my-testsuite.json
   # Or add pytest tests and reference in proposal testsuite_refs
   ```

4. **Gate the proposal**:
   ```bash
   tm gate proposal proposals/my-proposal.json
   ```

5. **If gate passes**: Submit PR with proposal, patch, and tests.

6. **If gate fails**: Follow failure handling above (fix or version bump + rationale + new tests).

### PR Checklist (must tick all)

- [ ] **Rule 1** — AI outputs are candidates only; they pass `tm artifacts verify` and regression tests before being accepted or patched.
- [ ] **Rule 2** — Every behavioral change is an explicit artifact/patch change (no implicit prompt tweaks or code branches).
- [ ] **Rule 3** — Each intent/spec has regression tests; new specs pass old tests, or version bump + explanation + new tests added.
- [ ] **Rule 4** — Large intent splits declare `trace_links.parent_intent` / `trace_links.related_intents`; tools can verify traceability.
- [ ] **Rule 5** — No runtime self-authorization; all resource effects go through PolicyGuard or approval token (e.g., `tm controller cycle --approval-token` when needed).

### PR Checklist (Proposal-driven changes)

For PRs that include proposals, patches, or intent/policy/workflow changes:

- [ ] **Proposal created** — Proposal artifact follows [Proposal v0.1 spec](specs/proposal-v0.1.md) and uses `templates/proposal_v0.1.json` as starting point.
- [ ] **Gate passed** — `tm gate proposal <proposal.json>` returns pass (or evolving warnings are explained in PR).
- [ ] **Patch included** — Patch artifacts referenced by proposal exist and are valid.
- [ ] **Tests included** — Test suites referenced by proposal exist and cover the change.
- [ ] **Impact declared** — `impacted_intents` in proposal lists all affected intent IDs.
- [ ] **Risk assessed** — `risk` field is set appropriately (`low`/`medium`/`high`).
- [ ] **Rationale provided** — `summary` and optionally `rationale` explain why the change is needed.
- [ ] **Failure handling** — If gate reported hard/compat failures, either fixed or version bumped + rationale + new tests added.
