# TraceMind K-Ontology v0.3 — AgentNetwork additive upgrade

**Version**: tracemind.io/v0.3
**Status**: Specification (Phase 6 Stage 6-1)
**Scope**: Additive upgrade of [K-Ontology v0.2](k-ontology-v0.2.md). Introduces a new artifact kind for **describing a star-topology agent network**: one center bundle aggregating governance, N leaf bundles each running a MAPE-K cycle. v0.2 ➜ v0.3 is purely additive — every v0.2 artifact remains valid under v0.3 schemas.

---

## 1. Scope & Non-Goals

### In scope (v0.3)

- New artifact kind: **`AgentNetwork`** — describes a star-topology composition of one center AgentBundle + N leaf AgentBundles + per-edge governance contracts (KPI keys reported by the leaf, patch kinds the center may dispatch back)
- Reserved `tree` topology value (enum only — no schema or runtime support yet; v0.3 implementations MUST reject `topology=tree`)
- v0.2 → v0.3 backward compatibility: **every v0.2 artifact remains valid under v0.3 schemas** (no breaking changes; only optional fields and one new artifact_type enum value)

### Out of scope (v0.3)

- **Transport implementations** — `Transport` Protocol semantics + `HttpTransport` / `FileQueueTransport` live in `tm/transport/` and are owned by Stage 6-2, not by this spec
- **Network-level Kripke verification** — the `joint_verify` machinery and `peer()` predicate syntax are owned by Stage 6-4
- **Cross-node evidence chain runtime** — `ProofReportBody.peer_chain_ref` / `EscalationReportBody.peer_node_id` are already reserved in v0.2 (since Phase 5 Stage 5-2 task 2.5); v0.3 does not change those schemas. Cross-node chain assembly is Stage 6-3
- **Dynamic topology changes** — adding / removing / re-electing nodes at runtime. v0.3 networks are static; mutation goes through governance (ProposedChangePlanBody)
- **Mesh / p2p** — explicitly excluded by Phase 6 invariant #6 (topology discreteness)
- **Tree topology** — enum-reserved only, not implemented

### Principle

v0.3 is purely **additive**:

- One new value in `ArtifactType` enum: `agent_network`
- One new artifact body schema: `schemas/v0/agent_network.json`
- One new in-runtime AST schema: `AgentNetworkSpec` in `tm/artifacts/schema.py`
- No envelope structural changes; no canonicalization changes; no existing schema field changes

---

## 2. New artifact kind: `AgentNetwork`

An AgentNetwork is a **first-class artifact** describing how one center AgentBundle coordinates N leaf AgentBundles via a typed edge contract. It does **not** embed the bundles — it references them by `artifact_id`, identical to how IntentBody references PropertyPattern in v0.2 (cf. Proposal v0.1 §3 patch-by-ref design philosophy).

### Body schema (minimal v0.3 form)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `network_id` | string | yes | Stable identifier; recommended namespacing `network.<domain>.<purpose>`, e.g. `"network.governance.cross_domain"` |
| `topology` | enum: `star` \| `tree` | yes | v0.3 only `star` is supported; `tree` is enum-reserved (validator MUST reject) |
| `center_bundle_ref` | string | yes | Artifact ID of the center AgentBundle (the L2 governance / aggregation node) |
| `leaf_bundle_refs` | array of string | yes | Artifact IDs of leaf AgentBundles (≥1). MUST NOT contain `center_bundle_ref` |
| `edges` | array of `Edge` | yes | Per-edge contract: which KPIs the leaf reports up, which patch kinds the center may dispatch down. ≥1 entry. Every leaf MUST have exactly one edge `from` ≡ leaf and `to` ≡ center |
| `transport_default` | enum: `inprocess` \| `http` \| `file_queue` | yes | Default transport for all edges; individual edges may override (`Edge.transport`) |
| `description` | string | no | Free-form prose |
| `metadata` | object | no | Free-form attribution / tags |

#### `Edge` object

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `from` | string | yes | Source bundle ref (a leaf in `leaf_bundle_refs`, or `center_bundle_ref` for center-down patches) |
| `to` | string | yes | Destination bundle ref. For `topology=star`, MUST be either the center or a leaf, and either `from` or `to` MUST equal `center_bundle_ref` (no leaf-to-leaf edges) |
| `kpi_keys` | array of string | yes (≥1) | KPI identifiers the leaf reports to the center. Names must match `[a-z][a-z0-9_.]*` |
| `allowed_patches` | array of string | yes (≥0, defaults to empty) | Patch kinds the center may dispatch via this edge (e.g. `policy_override`, `pause_capability`). Empty means leaf is read-only from the center's perspective |
| `transport` | enum (same as `transport_default`) | no | Overrides `transport_default` for this edge |
| `description` | string | no | Free prose |

### Worked example (anti-sybil + K8s HPA fairness star)

```yaml
envelope:
  artifact_id: network.cross_domain.demo.v1
  status: candidate
  artifact_type: agent_network
  version: v0.3
  created_by: human:phase-6-demo
  created_at: 2026-05-19T20:00:00Z
  body_hash: ""
  envelope_hash: ""
  meta: {}
body:
  network_id: network.cross_domain.demo
  topology: star
  center_bundle_ref: bundle.governance.cross_domain.v1
  leaf_bundle_refs:
    - bundle.anti_sybil.v1
    - bundle.k8s_hpa_fairness.v1
  edges:
    - from: bundle.anti_sybil.v1
      to: bundle.governance.cross_domain.v1
      kpi_keys: [sybil_burst_rate, quarantine_pending_count]
      allowed_patches: [policy_override]
    - from: bundle.k8s_hpa_fairness.v1
      to: bundle.governance.cross_domain.v1
      kpi_keys: [tenant_starvation_seconds, noisy_neighbor_count]
      allowed_patches: [policy_override, pause_capability]
  transport_default: http
  description: |
    Cross-domain governance star. Center aggregates fairness/safety KPIs from
    two unrelated leaf domains (anti-sybil + K8s HPA fairness). Demonstrates
    that the same PropertyPattern library can underpin both leaves without
    modification (cf. fablenet-ops tracemind-demos cross_domain proof).
```

---

## 3. Governance lifecycle

AgentNetwork follows the standard K-Ontology lifecycle (Stage 5-1 §3 governance baseline):

1. **candidate** — author writes an AgentNetwork body and submits it (file or API). `tm artifacts verify` runs schema validation + lint (see §4) + governance checks (see §5)
2. **accepted** — once verified, the envelope is hashed and frozen. Subsequent edits MUST go through `ProposedChangePlanBody` (just like every other accepted artifact)
3. **superseded** — replaced by another AgentNetwork artifact referenced by a ProposedChangePlanBody

Two governance constraints unique to AgentNetwork:

- **Center authority**: the center bundle is the canonical policy authority. Any `allowed_patches` value must correspond to a patch kind the center bundle declares it can produce
- **No leaf-side patches**: `allowed_patches` on a center-to-leaf edge represents patches the *center* may send down to the *leaf*; leaves never patch the center. Verifier MUST reject any AgentNetwork that puts `allowed_patches` on a leaf-to-center edge

---

## 4. Lint rules

`tm/lint/agent_network_lint.py` exposes `lint_agent_network(body)` returning `list[LintIssue]` (same shape as `lint_property_pattern` in v0.2). Issue codes:

| Code | Severity | Trigger |
|------|----------|---------|
| `AN_TOPOLOGY_UNSUPPORTED` | error | `topology=tree` (reserved but not implemented) |
| `AN_CENTER_IN_LEAVES` | error | `center_bundle_ref` also appears in `leaf_bundle_refs` |
| `AN_LEAF_EMPTY` | error | `leaf_bundle_refs` is empty |
| `AN_LEAF_DUPLICATE` | error | duplicate entries in `leaf_bundle_refs` |
| `AN_EDGE_EMPTY` | error | `edges` is empty |
| `AN_EDGE_UNKNOWN_NODE` | error | `edge.from` / `edge.to` not in `{center} ∪ leaves` |
| `AN_EDGE_LEAF_TO_LEAF` | error | for `topology=star`: edge connects two leaves (neither endpoint is the center) |
| `AN_EDGE_SELF_LOOP` | error | `edge.from == edge.to` |
| `AN_EDGE_LEAF_PATCHES_CENTER` | error | edge whose `from` is a leaf and `to` is the center has non-empty `allowed_patches` (leaves never patch the center) |
| `AN_EDGE_KPI_EMPTY` | error | `edge.kpi_keys` is empty (every edge must declare at least one KPI it carries) |
| `AN_EDGE_KPI_NAME` | error | `kpi_keys` entry does not match `[a-z][a-z0-9_.]*` |
| `AN_EDGE_TRANSPORT_UNKNOWN` | error | `edge.transport` not in `{inprocess, http, file_queue}` |
| `AN_LEAF_MISSING_EDGE` | warning | a leaf in `leaf_bundle_refs` has no outgoing edge to the center |

These are the **internal-consistency** lints. Cross-artifact lints (checking that `center_bundle_ref` / `leaf_bundle_refs` actually resolve to AgentBundle artifacts in some store, and that each bundle's monitor outputs include the declared `kpi_keys`) are owned by Stage 6-3 once a network-aware registry view exists. v0.3 only enforces shape.

---

## 5. Verifier integration

`tm/artifacts/verify.py::_validate_agent_network` runs three layers:

1. **JSON schema** — `validate_agent_network_spec(raw_body)` calls the AST validator, catching malformed types / missing required / unknown enum values
2. **Topology lint** — `lint_agent_network(raw_body)` runs the rules in §4 and surfaces every `error`-severity issue
3. **Body hash determinism** — same canonicalization as every other artifact body (no special-casing)

Successful verification flips status to `accepted` and stamps the envelope just like Stage 5-1 PropertyPattern.

---

## 6. v0.2 → v0.3 Backward Compatibility

Promise: **every accepted or candidate v0.2 artifact validates byte-identical under v0.3 schemas.**

How:

- v0.3 only adds `agent_network` to `envelope.artifact_type` enum (13 values total instead of 12). All other envelope fields unchanged
- No existing schema's `additionalProperties` or `required` changes
- No existing canonicalization or hash function changes
- `ArtifactType` Python enum gains one new value; existing names / values unchanged
- Body factory dispatch (`_BODY_FACTORY` in `tm/artifacts/models.py`) extends, never replaces, existing entries

Regression: `tests/test_v02_compat_under_v03.py` instantiates every v0.2 body kind (IntentBody, PropertyPatternBody, ProofReportBody, EscalationReportBody, …) and asserts they remain valid under v0.3 file-level + AST schemas, with byte-identical canonicalized form.

---

## 7. Phase 6 → 7 hooks reserved in v0.3

The following hooks are deliberately written into v0.3 even though they are not consumed until Phase 7 (NL→Formal v2):

- `AgentNetwork.metadata` is **open** (`additionalProperties: true` in JSON, `Dict[str, Any]` in Python) — Phase 7 may attach LLM-related provenance (prompt hash / model version) without schema bumps
- `Edge.allowed_patches` is **typed as string array, not enum** — Phase 7 can grow the patch vocabulary without v0.3 reissue
- `transport_default` and `Edge.transport` use an explicit enum (`inprocess` / `http` / `file_queue`) — closed enum here is intentional; any future transport requires v0.4 schema bump (forces governance review)

---

## 8. References

- v0.2 spec: [`k-ontology-v0.2.md`](k-ontology-v0.2.md)
- v0.1 spec: [`k-ontology-v0.1.md`](k-ontology-v0.1.md)
- Phase 6 plan: [`../../../.plan/phase-6-agent-network.md`](../../../.plan/phase-6-agent-network.md)
- Reserved ProofReport cross-node fields (Phase 5 Stage 5-2 task 2.5): `tm/artifacts/schemas/v0/proof_report.json` (`peer_node_id`, `peer_chain_ref`)
- Reserved EscalationReport cross-node fields (Phase 5 Stage 5-2 task 2.5): `tm/artifacts/schemas/v0/escalation_report.json` (`peer_node_id`)
