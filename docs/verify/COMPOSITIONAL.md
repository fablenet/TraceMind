# Compositional verification (assume-guarantee) — Stage 7-V.1

> Parent plan: [`.plan/phase-7-stage-7-V-compositional-verification.md`](../../../.plan/phase-7-stage-7-V-compositional-verification.md)
> Requirement source: `orchestrator-core/docs/extra_requirements_of_phase7.md` Req1 (Kripke state explosion).
> Status: **semantics frozen (2026-06-14)**. Implementation lands in 7-V.2–7-V.7.
> Nature: a **TraceMind verification-kernel capability, orthogonal to the LLM**. Deterministic, zero-LLM, no network. It does *not* change what "verified" means — it changes how the same Kripke + CTL judgement is *unfolded* so it stays computable as requirements grow.

This document pins down the contract that the implementation tasks build on. Getting the **soundness boundary** right here is the whole point: the one failure we must never allow is a *false PASS* (claiming a property holds when it does not). Everything below is engineered so that compositional mode can only ever be faster, never wrong — and when it cannot guarantee that, it falls back to the existing monolithic product.

---

## 0. Scope and non-goals

In scope (MVP, 7-V):
- **Space convergence** via **assume-guarantee (A-G)** on the `star` topology: verify each leaf locally, abstract it to an interface, then verify the center against the abstractions instead of the full product.
- **Time convergence (cheap lever)**: make the existing `hash_mode="store"` partial-order-reduction (POR) effect explicit and measurable.
- A soundness boundary that is **declared, machine-checked, and self-enforcing** (out-of-class properties auto-fall-back).

Explicitly out of scope (deferred / research, may never ship):
- `checkpoint horizon` segmentation (7-V.5, marked research).
- New topologies (mesh / tree) — invariant 6 keeps us on `star` only.
- Any change to the CTL evaluator (`tm/verify/ctl.py`) or the bounded-BFS explorer semantics (see [`SEMANTICS.md`](SEMANTICS.md)).

Invariants this stage must hold (from the parent plan §7):
- **Inv 3** — the K-plane stays the verifier: still deterministic Kripke + CTL; A-G is a smarter unfolding, not a new oracle.
- **Inv 6** — topology discreteness: `star` only.
- **Design: degradable** — compositional is an *optimization*; any out-of-class or spurious result re-checks monolithically, so the verdict is always reproducible by the full product.
- **Inv 2** — LLM-replaceable / irrelevant: this is pure kernel; no LLM anywhere on the path.

---

## 1. The problem (why the product explodes)

Multi-component verification today builds the **asynchronous-interleaving full product** of N components and explores it with bounded BFS. The joint report aggregates per-formula verdicts over that product:

```200:208:TraceMind/tm/verify/joint.py
    verified: bool
    component_count: int
    component_ids: List[str]
    formulas: List[str]
    verdicts: List[JointVerdict]
    state_count: int
    edge_count: int
    deadlock_count: int
```

`network_verify` composes the bundles of an `AgentNetwork` (center + leaves) and calls straight into `joint_verify`:

```116:137:TraceMind/tm/verify/network.py
def network_verify(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
    formulas: Sequence[str],
    *,
    max_depth: int = 16,
    hash_mode: str = "full",
    project_counterexamples: bool = True,
) -> NetworkVerifyReport:
    """Verify CTL formulas over the joint product of an AgentNetwork."""
    if not formulas:
        raise ValueError("network_verify requires at least one formula")

    components, component_ids = resolve_bundle_adapters(network, bundles)
    report = joint_verify(
        components,
        formulas,
        component_ids=component_ids,
        max_depth=max_depth,
        hash_mode=hash_mode,
        project_counterexamples=project_counterexamples,
    )
```

Because interleaving lets exactly one component step at a time, the reachable joint-state count is bounded by the **product** of the component state spaces: `|joint| ≈ |C| · ∏ᵢ |Lᵢ|`. Add a leaf, or deepen one leaf, and the cost grows multiplicatively. For a star with many similar leaves this is the dominant Phase-7 scaling wall.

---

## 2. Assume-guarantee semantics (space convergence)

### 2.1 The convergence point

In a `star`, the **center C** is the spatial convergence point: leaves interact only *through* the center, never directly (no leaf↔leaf edges in v0.3). That structure is exactly what A-G exploits — a leaf's effect on the global property is fully captured by what it exposes to the center across its edge.

### 2.2 Contracts live on existing fields (no new artifact)

We reuse fields already present in the K-Ontology rather than inventing a DSL:

| A-G concept | Where it lives | Existing meaning we reuse |
|-------------|----------------|---------------------------|
| **Assumption `Aᵢ`** (what leaf `Lᵢ` presumes about its environment/center) | `PropertyPatternBody.applicable_conditions` | "conditions under which this property applies" — i.e. the environment premises |
| **Guarantee `Gᵢ`** (what `Lᵢ` promises across its interface) | CTL formula(s) over the leaf's `AgentNetworkEdge.kpi_keys` and declared outputs | the leaf→center edge already declares the KPI facts visible at the interface |
| **Interface fact set** | `AgentNetworkEdge.kpi_keys` | the typed, edge-visible KPIs |

```666:671:TraceMind/tm/artifacts/models.py
    source: str
    target: str
    kpi_keys: List[str]
    allowed_patches: List[str] = field(default_factory=list)
    transport: str | None = None
    description: str | None = None
```

`applicable_conditions` already exists on every pattern:

```417:417:TraceMind/tm/artifacts/models.py
    applicable_conditions: List[str] = field(default_factory=list)
```

> Authoring rule: `Aᵢ` and `Gᵢ` are written in the same CTL/predicate language the verifier already speaks (`has(<id>.<key>)`, etc.), so no new parser is needed and a contract is itself verifiable.

### 2.3 The three-step star procedure

For center `C` and leaves `L₁..L_N`:

```
(1) Local verify   : for each i, verify  Lᵢ ⊨ Gᵢ  under assumption Aᵢ
                     (run Lᵢ alone against the most-general environment
                      permitted by Aᵢ — NOT the product)
(2) Abstract       : replace each verified Lᵢ by  Absᵢ  — an over-approximation
                     that exposes only the Gᵢ / kpi_keys-relevant facts and
                     stutters/nondeterministically chooses on internal steps
(3) Center verify  : verify  C ∥ Abs₁ ∥ … ∥ Abs_N ⊨ P
                     and that C discharges each Aᵢ (center supplies the
                     environment Lᵢ assumed)
```

State-count intuition: from `|C|·∏ᵢ|Lᵢ|` down to `maxᵢ(local cost) + |C|·∏ᵢ|Absᵢ|`, with `|Absᵢ| ≪ |Lᵢ|` because `Absᵢ` tracks only interface predicates.

### 2.4 Soundness — why there is no false PASS

`Absᵢ` is constructed to **over-approximate** `Lᵢ`'s interface behavior: every concrete interface trace `Lᵢ` can produce is also a trace of `Absᵢ` (the abstraction adds behaviors, never removes them). Therefore:

```
behaviors_interface( C ∥ L₁ ∥ … ∥ L_N )  ⊆  behaviors_interface( C ∥ Abs₁ ∥ … ∥ Abs_N )
```

A **safety property** `P = AG p` is *subset-closed*: if it holds over the larger behavior set (the abstract composition), it holds over every subset — in particular the concrete composition. Hence:

> **PASS on the abstract composition ⟹ PASS on the concrete system.** No false PASS. ∎ (safety fragment, non-circular assumptions)

This is the only direction we rely on. The converse does **not** hold: the abstraction may admit a *spurious* counterexample (a trace possible for `Absᵢ` but not `Lᵢ`), i.e. a **false FAIL**. We handle that by re-checking, never by trusting it (§4).

### 2.5 Non-circularity requirement

A-G for safety is sound only when the assumption dependencies are **acyclic**. In a star MVP we restrict to a two-level, non-circular discharge:
- each leaf assumption `Aᵢ` constrains only the **center→leaf** interface (what the center feeds the leaf), discharged by `C` in step (3);
- each leaf guarantee `Gᵢ` constrains only the **leaf→center** interface, encoded into `Absᵢ`.

Mutual / circular A-G (leaf assumes another leaf's guarantee that in turn assumes the first) needs inductive (well-founded) reasoning and is **out of class → fallback** (§3, §4). The decomposer (7-V.3) detects cycles in the assumption graph and refuses to use compositional mode for the offending formula.

---

## 3. Interface abstraction (`Absᵢ`)

`Absᵢ` is produced by `interface_adapter_from_contract(...)` (7-V.2) and implements the *same* `ComponentAdapter` protocol the product already consumes, so it drops into `joint_verify` with zero plumbing changes:

```46:56:TraceMind/tm/verify/joint.py
class ComponentAdapter(Protocol):
    """Duck-typed protocol matching :class:`TraceMindAdapter`'s surface.

    Any object exposing these four methods can be composed via JointAdapter.
    The existing ``TraceMindAdapter`` satisfies this trivially.
    """

    def initial_state(self) -> State: ...
    def successors(self, state: State) -> List[Tuple[str, State]]: ...
    def is_deadlocked(self, state: State) -> bool: ...
    def enabled_steps(self, state: State) -> List[str]: ...
```

Construction rules (the over-approximation contract):
- **State projection**: `Absᵢ`'s store keeps only keys in `kpi_keys` ∪ predicates referenced by `Gᵢ`. All internal leaf state is dropped.
- **Internal steps → stutter / nondeterminism**: any concrete step that does not change an interface fact becomes a self-loop (stutter); any concrete branching over interface facts that `Gᵢ` permits becomes a nondeterministic choice. The abstraction must admit *at least* every interface transition `Lᵢ` can make.
- **Bounded by `Gᵢ`**: the only constraint trimming the nondeterminism is `Gᵢ` itself (already locally verified in step 1). The abstraction never assumes anything `Gᵢ` did not establish.

Soundness obligation (tested in 7-V.2): for the property class in scope, `Absᵢ` must be a **simulation over-approximation** of `Lᵢ` on the interface alphabet — the test battery exhibits concrete interface traces and asserts each is reproducible by `Absᵢ` (no concrete behavior is excluded).

---

## 4. Supported property classes and fallback

The decomposer (7-V.3) classifies each formula before choosing a mode. The boundary is **declared and CI-guarded** so we never silently answer outside the sound fragment.

| Property class | Compositional? | Handling |
|----------------|----------------|----------|
| Safety / invariant: `AG p` | ✅ sound | A-G discharge (§2) — MVP primary target |
| Decomposable: conjunction of per-component obligations ∧ a center obligation | ✅ sound | decomposer assigns each conjunct to a leaf `Gᵢ` or to the center-abstract check |
| Single-component formula (refs one component's facts only) | ✅ sound | discharged as that leaf's local `Gᵢ` (step 1), no center product needed |
| Single-component reachability: `EF p` (refs one component) | ✅ sound | exact local verify (no product); `EF` witness path survives in the product |
| Liveness / existential cross-component: `EF`, `AF`, `EG`, `EX` over multiple components | ⛔ out of class | **auto-fallback monolithic** (see §4.1) |
| Fairness / conditional-liveness: `AG(p → EF q)` (eventually nested under `AG`), incl. single-component | ⛔ out of class | **auto-fallback monolithic** (see §4.1) |
| Single-component liveness: `AF p` (refs one component) | ⛔ out of class | **auto-fallback monolithic** — local liveness ⇏ product liveness without scheduler fairness (§4.1) |
| Non-decomposable safety (irreducible cross-component coupling) | ⛔ | **auto-fallback monolithic** |
| Cyclic assumption dependency (§2.5) | ⛔ | **auto-fallback monolithic** |

Two distinct fallback triggers, both ending in the full product so the result is always defensible:

1. **Out-of-class (decided up front)** — the formula is not in the sound fragment, or its assumption graph is cyclic. Compositional mode is not attempted for that formula; it is verified monolithically and listed in `fallbacks` with a reason.
2. **Spurious FAIL (decided after)** — the abstract composition reports a counterexample. Because the abstraction is wider, this may be a false FAIL. That specific formula is **re-verified monolithically** to confirm or refute. The monolithic verdict is authoritative; the recheck is recorded in `fallbacks`.

> Net effect: a compositional `verified=True` is sound for the in-class fragment, and a compositional `verified=False` is always confirmed by the full product. Out-of-class properties get the exact same answer they get today.

### 4.1 Why fairness / liveness stays monolithic (the boundary, made explicit)

The `fairness` PropertyPattern category compiles to the **response shape**
`AG(enforcement → EF mediation) ≡ AG(!has(enforcement) || EF has(mediation))`
— an *eventually* (`EF`/`AF`) nested under `AG`. The classifier
(`classify_formula`) routes every formula containing an eventually to
`OUT_OF_CLASS → monolithic`, and records a precise reason. There are **two
independent soundness obstructions**, either of which alone is disqualifying:

1. **∃-abstraction is not subset-closed for eventualities.** §2.4's no-false-PASS
   argument works because safety (`AG p`) is subset-closed: holding over the
   *larger* abstract behavior set implies holding over the concrete subset. An
   eventually is the opposite — `EF`/`AF` over a *superset* of traces can hold
   while the concrete subset does not. Discharging `EF`/`AF` against `Absᵢ` would
   risk a **false PASS**, the one outcome the whole design forbids.

2. **Single-component liveness does not lift to the unfair async product.** Even
   with *no* abstraction (verifying the exact leaf locally — the trick that makes
   single-component *safety* and *reachability* exact), liveness still fails to
   transfer. The product interleaves components asynchronously with **no fairness
   constraint**, so it admits schedules that **starve** a component (it never
   takes a step). A property like `AF has(leafA.ready)` can be true on `leafA`
   alone yet false in the product along a starving path. So local liveness ⇏
   product liveness.

Single-component **safety** (`AG p`) and **reachability** (`EF p`) *do* lift
exactly (stuttering by other components preserves `AG`; and an `EF` witness path
still exists in the product by letting the component run), which is why those are
in-class. Liveness/fairness is not, and is verified on the authoritative
monolithic product — the same answer as today, now with a self-documenting
fallback reason instead of a generic one. Adding sound compositional
fairness/liveness would require modeling explicit **scheduler fairness** in the
joint adapter (e.g. weak/strong fairness constraints), which is deferred
research, not part of this fragment.

---

## 5. Time convergence (POR now, horizon later)

### 5.1 POR (MVP, cheap) — made explicit and measured (7-V.5)

`hash_mode="store"` hashes only the store, merging states that differ only in
the order pending/done/events were produced (see [`SEMANTICS.md`](SEMANTICS.md)).
`tm/verify/por.py::measure_por` makes this lever explicit: it explores the same
product under `full` and `store` hashing and reports
`PORMeasurement{full_state_count, store_state_count, states_eliminated,
state_reduction_ratio, …}`. It is **verdict-free** — it only counts, so it can
never change a result.

**Honest finding (important).** `JointAdapter._project` already canonicalizes
each component's event/pending/done in *component order*, so naive
cross-component interleaving does **not** produce distinct full-states — that
reduction is already structural. store-hash therefore yields *additional*
reduction only where the **same store is reachable with different pending/done
queues** (e.g. two order-independent steps writing the same key: a "diamond"
collapses 5→2 states, ratio 0.6). On products that are already canonical the
measured reduction is honestly `0`. The dominant state savings in TraceMind come
from the **compositional abstraction** (§2–4), not from store-hash POR.

**Soundness caveat (declared).** store-hash is sound only for properties over
store facts (`has(...)`). Formulas referencing `pending(...)` / `done(...)` /
`Terminal` depend on the scheduling state store-hash collapses and MUST use
`hash_mode="full"`. POR is an optimization/measurement, never a verdict change.

### 5.2 Checkpoint horizon (deferred / research)

Segment the BFS at plan milestone/cut predicates, verify within a segment,
summarize boundary states, then continue. Flagged research to keep 7-V from
ballooning; **MVP ships POR measurement only** and does not implement horizon
segmentation.

---

## 6. API surface (additive; Stage 6-4 zero regression)

The contract the implementation will expose (frozen here so 7-V.2–7-V.6 agree):

- `joint_verify(...)` / `network_verify(...)` gain `mode: Literal["monolithic","compositional"] = "monolithic"`. **Default `monolithic` ⟹ existing behavior byte-identical**; current callers (`network.py`, CLI `verify_network.py`) are untouched.
- New module `tm/verify/compositional.py`:
  - `interface_adapter_from_contract(bundle, *, kpi_keys, guarantees) -> ComponentAdapter` — the §3 over-approximation.
  - `assume_guarantee_verify(network, bundles, formulas, *, max_depth=16, hash_mode="store") -> CompositionalReport`.
- Report extension (**additive — no field removed**) on `JointReport` / `NetworkVerifyReport`:
  - `mode` — which mode actually ran.
  - `local_verdicts` — per-leaf local `Gᵢ` results (step 1).
  - `abstraction_stats` — `|Lᵢ|` vs `|Absᵢ|` per leaf.
  - `monolithic_state_count` vs `compositional_state_count` — the headline reduction.
  - `fallbacks` — which formulas fell back and why (out-of-class vs spurious-FAIL recheck).

CLI (7-V.6): `tm verify network --mode compositional`, defaulting to `monolithic`.

---

## 7. Determinism & invariants

- **Deterministic**: contract resolution, abstraction construction, decomposition, and BFS are all pure functions of the artifacts + formulas; repeated runs are byte-identical. No LLM, no network, no clock.
- **Degradable**: every uncertainty resolves to the monolithic product, which is the existing trusted oracle — so compositional mode can be disabled entirely without changing any verdict.
- **Honest reporting**: `fallbacks` and `abstraction_stats` make it visible *when* the optimization helped and *when* it punted, so a reviewer never mistakes "fell back" for "compositionally proven".

---

## 8. Acceptance (the 7-V DoD this spec commits to)

- anti-sybil + K8s-HPA star demo: compositional `state_count` materially below monolithic, with **per-formula verdicts identical** to monolithic.
- **Zero regression**: default-`monolithic` output byte-identical; Stage 6-4 suite stays green.
- Soundness: abstraction over-approximation has test backing (no false PASS); non-decomposable / out-of-class formulas auto-fall-back and appear in `fallbacks`.
- POR: store-hash mode yields a quantified state-reduction number.

---

## 9. Glossary

- **A-G (assume-guarantee)** — prove each component under an assumption about its environment, then compose the guarantees; here, leaves are verified locally and the center against their abstractions.
- **Over-approximation** — an abstraction whose behavior set is a superset of the concrete one; preserves safety properties downward (no false PASS).
- **Decomposable formula** — one whose obligation splits into per-component parts plus a center part, each dischargeable without the full product.
- **Fallback (monolithic)** — verifying with the existing full-product `joint_verify`, used whenever compositional soundness is not guaranteed.
- **POR (partial-order reduction)** — collapsing scheduling-equivalent interleavings; here realized cheaply via `hash_mode="store"`.

References: [`SEMANTICS.md`](SEMANTICS.md) · `tm/verify/joint.py` · `tm/verify/network.py` · K-Ontology v0.3 (`docs/specs/k-ontology-v0.3.md`).
