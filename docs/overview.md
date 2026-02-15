# TraceMind · Developer Docs

This doc set focuses on running, extending, and operating TraceMind Core Runtime.

## What’s in this milestone
- T-LLM-01: Provider-agnostic LLM client + `ai.llm_call`
- T-POLICY-02: MCP-backed policy adapter with graceful fallback
- T-DOC-03: Documentation & recipes (this package)

**Zero-conflict** promise: these docs only add files; no code changes required.

## Specs

- **[K-Ontology & Canonical AST v0.1](specs/k-ontology-v0.1.md)** — Three-layer ontology (data/meta/meta-meta), canonical AST envelope, type catalog, and canonicalization rules for M1.
- **[Policy determinism (M2 · ISSUE-003)](policy_determinism.md)** — Immutable rules for policy evaluation order, conflict strategy (first-wins), condition AST semantics, and canonical action_log.

## Governance Baseline (default constraints)

Contributors must follow the [Governance Baseline](governance/baseline.md) and the [Non-negotiable Governance Rules](CONTRIBUTING.md#non-negotiable-governance-rules) in CONTRIBUTING. These rules cover AI→candidate→verify→accept flows, explicit artifact diffs, patch lifecycle, intent traceability, and the ban on runtime self-authorization.
