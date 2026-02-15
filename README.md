# TraceMind — Governed Agent Runtime + Design-Time Verification Toolchain

TraceMind helps you build AI-assisted systems that **run as agents**, but **do not drift**.
It separates **proposal** from **execution**, and treats governance as a first-class product feature.

TraceMind is designed for scenarios where a non-technical customer can express intent,
and the system can iteratively compile that intent into runnable units **without violating boundaries**.

---

## What Problem TraceMind Solves

AI-assisted products often fail in the same ways:

- The system executes actions without explicit checks.
- “Intent” is ambiguous and becomes a moving target.
- Runtime behavior drifts over time and becomes hard to audit or roll back.
- Multi-step workflows become opaque and ungovernable.

TraceMind addresses this by introducing a strict workflow lifecycle:

> **Intent → Compile → Verify → Run → Trace → Diagnose → Patch (Approved) → Iterate**

The goal is not to make AI “smarter”.
The goal is to make AI-enabled systems **governable, auditable, and safe-by-design**.

---

## Core Product Idea: Two Planes

TraceMind has two planes that work together:

### 1) Design-Time Plane (Offline / Iteration)

This is where correctness and “one-meaning” intent are enforced.

- Users (or AI as a helper) produce an **Intent**: what should happen (goals, constraints, preferences).
- The system compiles Intent + Plugins + Policy into a runnable **WorkflowPolicy**.
- Verification rejects plans that violate policy and produces explanations/counterexamples.
- Improvements happen through explicit, versioned **PatchProposals** and approvals.

Design-time is where you prevent drift before anything runs.

### 2) Runtime Plane (Online / Execution)

This is where the system runs as **agents**.

- An **Agent** is a runtime module assembled from:
  - declared plugins (capabilities),
  - a verified workflow policy,
  - enforced governance policy.
- The runtime executes the verified workflow and emits immutable **traces** as evidence.
- Multiple agents can be connected into an **agent network** via events/messages, while still enforcing local and shared policies.

Runtime is where you execute safely and produce evidence.

---

## Key Terms (Product Definitions)

### Artifact

An artifact is a versioned, validated, auditable record (YAML/JSON) that the system treats as truth.
Artifacts are the backbone of iteration and governance: no hidden state, no “magic decisions”.

Typical artifacts include (names may evolve as the project stabilizes):

- Intent (goal/constraints/preferences)
- Policy (invariants/guards/liveness)
- Capability specs (what plugins can do + side-effects)
- WorkflowPolicy (compiled runnable unit)
- Execution trace (what actually happened)
- Patch proposal (how to change safely)

### Agent

An agent is **not** “autonomous” in the sense of self-authorizing or self-expanding.
In TraceMind, an agent is a runtime node that executes **verified** workflows under explicit policy.

### Plugin / Capability

A plugin declares what it can do, including inputs/outputs, emitted events, extracted state, and side-effects.
Undeclared behaviors are treated as non-existent.

### Policy

Policy defines enforceable boundaries:

- what must never happen,
- what requires guards/approval,
- what must eventually happen.

---

## What TraceMind Is / Is Not

**TraceMind is:**

- a governed agent runtime + an offline verification toolchain
- a workflow system where proposals are compiled and verified before execution
- evidence-first: every execution is traceable and replayable

**TraceMind is not:**

- a self-authorizing autonomous agent system
- a “prompt orchestration” tool that lets an LLM execute actions directly
- a runtime that silently adapts or changes rules without approval

---

## How a Typical “Completion” Looks (End-to-End)

1) A customer expresses a requirement (often ambiguous).
2) AI can help translate it into an **Intent** draft.
3) Intent goes through **automatic validation**:
   - schema validity
   - semantic validity (no hidden execution instructions)
   - feasibility pre-check (is there a governed solution?)
4) The system compiles a **WorkflowPolicy** from declared plugins + policies.
5) Verification runs:
   - policy checks
   - bounded simulation / counterexamples (as supported)
6) Runtime executes the verified workflow as an agent.
7) Execution emits **trace** and an integrated state report.
8) If results drift from expectations, the system generates a **PatchProposal**.
9) PatchProposal must be approved and versioned before affecting runtime.

---

## Repository Structure (as of today)

- `tm/` — core runtime modules and tooling (artifacts, capabilities, composition, verification, governance)
- `docs/` — design notes and the evolving semantic foundation ([overview](docs/overview.md); specs include [policy determinism M2](docs/policy_determinism.md))
- `examples/` — minimal reference flows to exercise the closed loop
- `tests/` — validation and governance tests

---

## Development Status (Phase 1)

TraceMind is in a phase where the system is being unified into a complete workflow:

- preserving a real runtime agent architecture,
- integrating a design-time compiler/verifier loop,
- making Intent validation and governance explicit and deterministic.

Breaking changes are expected while the semantic foundation is finalized.

---

## Contribution Rules (Non-Negotiable)

- Do not let AI trigger runtime actions directly.
- Do not execute side-effectful plugins without policy verification and required guards.
- Every feature must map to an explicit artifact or rule.
- If you cannot explain how a change is governed, do not implement it.

> TraceMind optimizes for governance, not autonomy.  
> Constraints come before capabilities.

---

## Composer v0 (`tm compose`)

The composer now enumerates a small, deterministic set of templates (e.g. `compute.process → validate.result → external.write`) and scores every candidate twice using the conservative/aggressive weight sets described in the Phase‑1 plan. Each mode minimizes a cost function built from normalized metrics (side effects, rollback risk, nondeterminism, guards coverage, complexity) so that the same intent/policy/catalog always yields the same ranking and rejection reasons.

Use `tm compose --intent intent.yaml --policy policy.yaml --catalog catalog.json --modes conservative,aggressive --k 1 --explain` to:

1. Emit the top workflow policy for each mode with guard annotations, transitions, and the selected template metadata.
2. Print or save a JSON explanation that includes raw/normalized metrics, weight assignments, cost terms, rationale sentences, checks (invariants/guards), deterministic signatures, and structured rejection entries (`MISSING_CAPABILITY`, `GUARD_REQUIRED_BUT_MISSING`, `POLICY_INVARIANT_VIOLATION`, `UNSATISFIABLE_INTENT`, etc.).

Rejection evidence includes the offending invariant, capability, and state snapshot so downstream tools can explain why a candidate was filtered before execution.

---

## Verifier v0 (`tm verify workflow`)

Use `tm verify workflow --workflow workflow.yaml --policy policy.yaml --capabilities caps/*.yaml --json` to run a static-invariant check plus a simulation-lite replay of the template. When an invariant fails, the CLI emits a structured `counterexample` containing the step sequence, the events produced, the violated invariant ID, the triggering condition, and the state snapshot at the failing step. The command exits non-zero for violations so it can block merges even if the workflow was hand-edited outside the composer.

---

## Runtime v0 (`tm runtime run-workflow`)

`tm runtime run-workflow --workflow workflow.yaml --policy policy.yaml --capabilities caps/*.yaml --guard-decision guard_name=true --events event.happened --format json`

The runtime executor now enforces the same verification artifact and guard constraints before executing any workflow. It loads the composed `WorkflowPolicy`, reuses the `PolicySpec`/capability catalog, runs `WorkflowVerifier` to block unverified workflows, evaluates policy guards step-by-step, and emits a determined `ExecutionTrace` once every step has been processed (or rejected).

- Guarded steps are only executed when the required `guard_name` decision is `true`; otherwise the run fails fast, writes a `guard-denied` entry, and records a structured violation (`guard:<name>`).
- Execution traces include the `trace_id`, `workflow_id`, final `state_snapshot`, `violations`, and `metadata.guard_decisions` plus any extra `--events` appended to the trace. Steps that complete emit their final event (e.g. `external.write.done`), while additional events show up as `unit: event` entries.
- The runtime command exits non-zero for either verification failures (prints the same counterexample payload as `tm verify workflow`) or invalid artifacts/guard refusal, making it safe to gate agent execution on the CLI.

Use `tm runtime report-state --workflow workflow.yaml --policy policy.yaml --capabilities caps/*.yaml --trace trace.json --format json` to turn an `ExecutionTrace` back into an `IntegratedStateReport`. The command recreates the semantic state by replaying each step’s `state_extractors`, applies policy invariants, and, when violations occur, emits the rule ID, the list of triggering events, and the blamed capability/guard inside a schema-validated report that downstream tooling (e.g. PatchProposal authors) can consume.

Use this command to ensure every WorkflowPolicy that reaches the runtime is policy-bound, guarded, and fully explainable via its ExecutionTrace.

---

## Patch Governance v0 (`tm patch`)

PatchProposals now live in a deterministic on-disk store under `.tm/patches/`. Run `tm patch propose --from patch.json --created-by you --target policy --target-ref policy.json --kind tighten_guard --rationale "..." --expected-effect "..." --risk-level medium` to materialize a `DRAFT` proposal in `.tm/patches/proposals/<proposal_id>.json`. `tm patch submit <proposal_id>` validates the referenced artifact and moves the status to `SUBMITTED`. `tm patch approve <proposal_id> --actor reviewer --reason "safe"` attaches an approval event and marks it `APPROVED`. Finally, `tm patch apply <proposal_id> --out-dir .tm/artifacts` bumps the artifact version, emits a new JSON file, records governance metadata, writes an application record under `.tm/patches/applied/`, and marks the proposal `APPLIED`.

Each proposal carries metadata such as `target_artifact_type`, `target_ref`, `patch_kind`, `risk_level`, and optional review notes. The CLI enforces immutability once status leaves `DRAFT`; to change a patch, create a new proposal. Operators can inspect `.tm/patches/index.json` to see the status stream before the runtime ever consumes a new policy or workflow.

---

## Rerun Pipeline (`tm rerun`)

`tm rerun --intent intent.yaml --policy policy.yaml --catalog caps/catalog.json --mode conservative --guard-decision external-write-approval=true` executes the Phase‑1 flow in one shot: it validates the Intent, composes the workflow (`--mode` selects the scoring weights), verifies the composed workflow, and finally runs it through the guarded executor with explicit guard decisions. The command emits a JSON payload that contains each stage’s output plus the final `ExecutionTrace`, making it easy to reproduce how a change progresses from intent to trace without hand‑wiring CLI invocations.
