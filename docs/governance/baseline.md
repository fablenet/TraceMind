# Governance Baseline

This document codifies five non-negotiable governance rules (贡献宪法) for TraceMind. All contributors must adhere to these rules when producing or modifying intents, artifacts, AI outputs, or runtime behavior.

---

## The Five Hard Constraints

### 1. AI 永远只产出候选 (AI Only Produces Candidates)

**中文**: AI 输出必须先经过 schema/验证/回归测试，才能变成 accepted 或进入 patch 管道。

**English**: AI outputs must first pass schema validation, verification, and regression tests before they can become `accepted` or enter the patch pipeline. AI never directly writes `status: accepted` artifacts; the verifier and governance toolchain do.

### 2. 每次“通过”必须是显式 artifact 变更 (Every "Pass" Must Be Explicit Artifact Change)

**中文**: 不能靠隐式 prompt 变化、隐式代码分支来改变行为；必须形成可 diff 的 artifact/patch。

**English**: Do not rely on implicit prompt changes or implicit code branches to change behavior. Every behavioral change must result in an explicit artifact or patch that can be diffed and audited. Use `tm artifacts diff` to compare before/after.

### 3. 每个 intent 必须绑定一组可回归的测试 (Each Intent Must Bind to Regression Tests)

**中文**: 新规格必须通过旧测试；若不通过，只能通过“版本大化 + 解释 + 新测试补齐”推进（对齐 ConsistencyReport.C3 思路）。

**English**: New specs must pass existing tests. If they do not, the only allowed path is (1) version bump (major or schema version increment), (2) written explanation, and (3) new tests added to cover the new behavior. Aligns with ConsistencyReport C3: regressions are forbidden unless explicitly versioned and documented.

### 4. 大意图拆分必须可追溯 (Large Intent Splits Must Be Traceable)

**中文**: 父子关系必须是显式字段（可拓扑/可检测），并能被工具校验。优先复用 TraceLinks.parent_intent/related_intents。

**English**: Parent-child relationships must be explicit fields, topologically detectable, and verifiable by tools. Reuse `trace_links.parent_intent` and `trace_links.related_intents` for decomposition and sibling references.

### 5. 运行时不允许“自授权” (No Runtime Self-Authorization)

**中文**: 任何 resource effect 必须经过 PolicyGuard 或审批 token。

**English**: Every resource effect must go through PolicyGuard or an approval token. Runtimes may not self-authorize; human or governance approval is required for side effects on resources.

---

## Standard Workflow

```
AI / human → candidate artifact
     ↓
tm validate (flows/policies/artifacts)
     ↓
tm artifacts verify <path>
     ↓
pytest -q tests/
     ↓
tm artifacts accept <path> --out <dir> --registry <registry>
     ↓
registry (.tracemind/registry.jsonl)
```

### Copy-paste commands

```bash
# Validate flows/policies for conflicts
tm validate --flows flows/*.yaml --policies policies/*.json

# Validate artifacts for schema conformance
tm validate specs/examples/artifacts_v0/*.yaml

# Verify a candidate artifact
tm artifacts verify path/to/candidate.yaml

# Run regression tests
pytest -q tests/

# Accept verified candidate into registry
tm artifacts accept path/to/candidate.yaml --out .tracemind/artifacts --registry .tracemind/registry.jsonl
```

---

## Explicit Diff

Use `tm artifacts diff` to compare two artifacts and ensure behavioral changes are traceable:

```bash
# Compare current vs previous artifact
tm artifacts diff current-artifact.yaml previous-artifact.yaml

# JSON output
tm artifacts diff current-artifact.yaml previous-artifact.yaml --json
```

Every meaningful change should be visible in the diff. If behavior changes without a corresponding artifact diff, the change violates Rule 2.

---

## Patch Lifecycle

For governed changes to policy, intent, workflow, or capability artifacts:

```bash
# 1. Propose a patch (creates draft PatchProposal)
tm patch propose --from patch.yaml \
  --created-by "author@example.com" \
  --target policy \
  --target-ref policies/main.json \
  --kind tighten_guard \
  --rationale "Prevent X" \
  --expected-effect "Y" \
  --risk-level low

# 2. Submit for review
tm patch submit <proposal_id>

# 3. Approve (reviewer)
tm patch approve <proposal_id> --actor "reviewer@example.com" --reason "Verified safe"

# 4. Apply (emit new artifact)
tm patch apply <proposal_id> --out-dir .tracemind/artifacts
```

---

## Intent Split: trace_links Example

When splitting a large intent into children, declare parent-child and sibling links:

```yaml
body:
  intent_id: TM-INT-0002
  title: "Child intent"
  goal: "Sub-goal of parent"
  trace_links:
    parent_intent: TM-INT-0000   # parent
    related_intents: [TM-INT-0001, TM-INT-0003]  # siblings
```

- `parent_intent`: The parent intent ID when this intent is derived from a larger one.
- `related_intents`: Sibling or related intent IDs for cross-reference and topology checks.

Tools can verify traceability by walking `parent_intent` and `related_intents` to detect cycles and coverage gaps.

---

## No Self-Authorization at Runtime

- **PolicyGuard**: Every resource effect declared in an agent's IO contract is evaluated against `meta.policy.allow` before execution. Denied effects abort the step and record `policy_decisions`.
- **Approval token**: When running controller cycles with resource effects, pass an explicit token:
  ```bash
  tm controller cycle --bundle accepted-bundle.yaml --report report.yaml --approval-token approved
  ```
- **HITL (Human-in-the-Loop)**: For high-risk patches or effects, use `tm/governance/hitl` and PatchStore approval flow. Runtimes must not bypass these gates.

---

## See Also

- [CONTRIBUTING — Non-negotiable Governance Rules](../CONTRIBUTING.md#non-negotiable-governance-rules)
- [How to Write an Intent](../how_to_write_intent.md)
- [IO Contract v0](../io_contract_v0.md)
- [Validation Guide](../validation.md)
