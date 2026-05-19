# Changelog

## [Unreleased]
- _None_

## [2.1.0] - 2026-05-19
### Phase 5: TraceMind generalization (universal control plane)
Phase 5 turns `trace-mind` into a domain-agnostic control plane that downstream
scenarios (FableNet anti-sybil, K8s HPA fairness, ...) consume as a library. All
additions follow the LLM-replaceability + adapter/scenario separation invariants;
the `tm/` core remains free of any FableNet-specific dependencies (enforced by
`scripts/check_core_independence.py`).

#### Stage 5-1 — Property Pattern schema (additive)
- New artifact: `PropertyPatternBody` (`tm/artifacts/schemas/v0/property_pattern.json`,
  models, validator, lint).
- `IntentBody` gains `pattern_ref` (links intents to pattern instances).
- `tm lint property-pattern` + governance rules for pattern review.
- v0.1 envelopes remain accepted under v0.2 (`tests/test_v01_compat_under_v02.py`).

#### Stage 5-2 — Meta/arbiter generalization
- Moved domain-neutral meta/arbiter logic from `fablenet-control` to `tm.control`
  (controller, convergence, escalation, kpi tracker, proof, cycle bridge, conflict).
- New artifacts: `ProofReportBody`, `EscalationReportBody`, joint verifier
  (`tm/verify/joint.py`) and policy conflict detector (`tm/policy/conflict.py`).
- Core-independence guard: `scripts/check_core_independence.py` +
  `tests/test_check_core_independence.py`.

#### Stage 5-3 — Pattern Library + non-LLM path
- Ship three seed patterns (`safety_no_x_amplifies_y`, `liveness_eventually_recovers`,
  `fairness_no_starvation`) under `tm/patterns/` with deterministic CTL compilation.
- `tm pattern list|show|instantiate` CLI and `instantiate_pattern()` API.
- Reusable pattern-assembled anti-sybil verification (`fablenet-control` migrated
  off bespoke CTL strings to pattern instances).

#### Stage 5-4 — NL → Pattern AI pipeline + KB + K8s adapter
- `tm.kb.case_corpus` — virtual aggregate view over registry / proof / escalation
  / change-plan artifacts; primary index by intent, secondary by pattern.
- `tm.kb.retrieval` — pluggable `Retriever` protocol; `PatternKeywordRetriever`
  (Jaccard), `CaseStructuredRetriever`, reserved `VectorRetriever` seam.
- `tm.compile.intent_to_bundle` — deterministic compiler from pattern-based
  `IntentBody` to `PlanBody` (PolicySet) + `AgentBundleBody` (MAPE-K skeleton),
  passing governance + IO-closure linters.
- `tm.steps.ai_propose_pattern` — NL → `PatternProposal` step with RAG-only
  (LLM-free) `fake` provider path, statically verified against LLM imports.
- `tm.kb.feedback` — synthesizes `ProposedChangePlanBody` patches from failed
  proofs / critical escalations (AI proposes, governance disposes).
- `extensions.k8s` — in-tree, scenario-free K8s plumbing (`FakeKubeApiServer`,
  `K8sObserveAdapter`, `K8sExecuteAdapter`) with domain-neutrality lint.

#### Tests
- +108 new in-tree TraceMind tests (Stage 5-1..5-4 combined). Full regression:
  837 passed / 2 skipped. Core-independence: 340 files scanned, zero FableNet
  references. Cross-domain proof: byte-identical pattern bodies across FableNet
  anti-sybil and K8s HPA fairness scenarios.

## [2.0.2] - 2026-02-15
- Docs: add constitution entry pages (ISSUE-001) — semantics.md, decisions.md, intent.md; nav in mkdocs and overview
- Docs(intent): fix parent/related to trace_links paths (trace_links.parent_intent, metadata.trace_links.*), forbid top-level fields

## [1.1.0] - 2025-10-13
- Add: trigger runtime (cron/webhook/filesystem adapters, `tm triggers run`, queue dispatcher)
- Add: daemon integration for triggers (`tm daemon start --enable-triggers`, consolidated `tm.daemon.run` entrypoint)
- Add: trigger configuration tooling (`tm triggers init|validate`, `templates/triggers.yaml`, `docs/triggers.md`)
- Update: README / daemon docs / smoke script with trigger workflows
- Fix: Windows filesystem trigger config parsing by switching tests to JSON generation
- Bump: project version to 1.1.0
