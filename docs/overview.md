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
- **[Trace format v0.1](specs/trace-format-v0.1.md)** — Trace JSONL event structure (ts, obs, state, context), replay output (one action_log per event, M2 canonical), replay diff (added/removed/changed, summary by intent_refs or rule_id).
- **[Intent tree v0.1](specs/intent-tree-v0.1.md)** — Intent node minimum fields (id, title/summary, trace_links), single authority for parent-child (trace_links.parent_intent), topology (acyclic, parent exists), leaf definition and requirements (success_criteria, tests coverage via M5.2).
- **[Proposal v0.1](specs/proposal-v0.1.md)** — Proposal ontology and semantic boundaries (M6.1): envelope (M1.1 canonical AST), minimal spec fields (impacted_intents, patch_refs, testsuite_refs, risk, summary, rationale), lint vs schema boundaries, relationship to gate (M6.2). [Example](specs/examples/proposal_v0.1.json).

## Constitution entry (ISSUE-001)

- **[Semantics](semantics.md)** — Canonical AST, rendering (parse/print), deterministic policy (priority/id, first-wins, applied:false), tests gate (hard/compat/evolving).
- **[Decisions](decisions.md)** — Governance baseline, gate pipeline order, what can evolve (hard/compat/evolving + major bump).
- **[Intent](intent.md)** — Intent tree structure, single root / acyclic, leaf success_criteria and tests coverage, validate & coverage commands.

## Governance Baseline (default constraints)

Contributors must follow the [Governance Baseline](governance/baseline.md) and the [Non-negotiable Governance Rules](CONTRIBUTING.md#non-negotiable-governance-rules) in CONTRIBUTING. These rules cover AI→candidate→verify→accept flows, explicit artifact diffs, patch lifecycle, intent traceability, and the ban on runtime self-authorization.

- **[Regression rules (硬约束 #3)](regression_rules.md)** — hard/compat/evolving definitions, change advancement rules (old tests must pass; else major bump + rationale + new tests), and how to run regression/gate (`pytest -q tests/`, `tm tests run`, `tm gate run` + report.json).
