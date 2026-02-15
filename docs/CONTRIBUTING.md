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

### PR Checklist (must tick all)

- [ ] **Rule 1** — AI outputs are candidates only; they pass `tm artifacts verify` and regression tests before being accepted or patched.
- [ ] **Rule 2** — Every behavioral change is an explicit artifact/patch change (no implicit prompt tweaks or code branches).
- [ ] **Rule 3** — Each intent/spec has regression tests; new specs pass old tests, or version bump + explanation + new tests added.
- [ ] **Rule 4** — Large intent splits declare `trace_links.parent_intent` / `trace_links.related_intents`; tools can verify traceability.
- [ ] **Rule 5** — No runtime self-authorization; all resource effects go through PolicyGuard or approval token (e.g., `tm controller cycle --approval-token` when needed).
