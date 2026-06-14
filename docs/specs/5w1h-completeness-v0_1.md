# TraceMind 5W1H 完整性契约 v0.1

**Version**: tracemind.io/5w1h/v0.1
**Status**: Specification（Phase 7 Stage 7-0 · Task 7-0.1）
**Scope**: 在现有 K-Ontology 之上定义一个**确定性、零 LLM**的「5W1H 完整性契约」——把一个需求（Intent，可选挂 Plan / AgentNetwork）映射到 Who/Why/What/When/Where/How 六个维度，判定每维 `satisfied / partial / missing / not_applicable`，并产出**模板化补全建议**（候选，非自动落地）。
**Non-Goal**: 不新造逻辑/DSL；不替代 CTL 验证；不做语义正确性判断（那是 `tm verify` 的事）。本契约只判「需求描述是否结构完整」。

---

## 1. 设计原则（与不变量对账）

| 原则 | 说明 |
|------|------|
| **确定性** | 完整性判定 100% 由规则得出，可重现、CI 可跑、无网络。`tm/intent/completeness.py` **禁止 import 任何 LLM 模块**（CI 静态守，Task 7-0.7）。守不变量 3。|
| **不新造逻辑** | 维度证据全部来自既有 artifact 字段；不引入新的形式语言。守不变量 3。|
| **AI 只产候选** | `suggestion` 是按 (维度, profile) 取的模板串，非 LLM 生成；且仅建议，不落地。守不变量 5。|
| **域可扩展** | 维度骨架领域无关；领域语义全进 *domain profile*。新域只加 profile 文件，`completeness.py` 与 CTL 零改动。|

---

## 2. 5W1H → K-Ontology 映射（核心事实：跨 artifact，非单层）

| 维度 | 主证据（artifact.field） | 判定规则 | base severity |
|------|--------------------------|----------|---------------|
| **Who** | `IntentBody.actors` | 非空 → `satisfied`；空 → `missing` | `error` |
| **Why** | `IntentBody.context` + `goal` | 两者非空 → `satisfied`；缺一 → `partial`；全缺 → `missing` | `error` |
| **What** | `IntentBody.goal` + (`inputs` ∪ `outputs`) | goal 非空且至少一侧 IO 非空 → `satisfied`；goal 有但 IO 全空 → `partial` | `error` |
| **When** | 挂 Plan 时 `PlanRule.triggers` 非空 ∨ 引用的 PropertyPattern `category` 含时序类（`liveness`）∨ `slot_fills` 内有 `when_*` 键 | 命中任一 → `satisfied`；无 Plan 且无时序 pattern → `missing`（reason 标注无取证源）| `warn` |
| **Where** | 挂 `AgentNetwork`（拓扑）∨ `IntentBody.slot_fills` 内 domain/`where_*` 标记键 | 命中 → `satisfied`；否则 `missing` | `warn` |
| **How** | `IntentBody.property_pattern_refs` 非空（可编译出 Plan/AgentBundle）∨ 直接挂 Plan | 命中 → `satisfied`；否则 `missing` | `error` |

**取证降级**：当判定某维需要的 artifact 未提供（如未挂 Plan 取 When/How、未挂 Network 取 Where），不抛异常——按规则给 `missing` 并在 `missing_reason` 标注「no linked Plan and no temporal pattern referenced」之类的可读原因。绝不静默通过。

`not_applicable`：仅当某 profile 显式把该维 severity 设为 `off` 时，结果记 `not_applicable`，不计入 errors/warnings。

---

## 3. Severity 与退出码

| severity | 含义 | 未 satisfied 时影响 |
|----------|------|---------------------|
| `error` | 该维必须完整 | 计入 errors；任一 error 维未 satisfied → **exit code 1** |
| `warn` | 建议完整 | 计入 warnings；不影响 exit code |
| `off` | 本 profile 不要求该维 | 结果 `not_applicable`，不计入 |

`partial` 在 `error` 维下也计入 errors（视为未满足）；在 `warn` 维下计入 warnings。

---

## 3b. 两阶段判定 + 不确定性闭合（用户确认 2026-06-13）

完整性判定分两阶段，匹配「交互前进 → 最后切结」的产品过程：

| 阶段 | mode | 散文处理 | 容忍度 | 用途 |
|------|------|----------|--------|------|
| **设计期** | `design`（B）| **确定性关键词启发式**：按 profile `vocabulary_hints` 词表扫 `context`/`goal` 散文，命中 When/Where 线索 → 记 `partial` | 宽松（允许 partial/missing）| 探索、展开、低摩擦 |
| **落地期** | `seal`（A）| **严格**：只认结构化字段 | 零容忍：每个必填维必须**被闭合** | 切结前的硬闸 |

> 启发式仍是**确定性**的（固定词表匹配，无 LLM），只是判定更宽松、可能误判 → 故仅用于设计期提示，不作落地依据。

**退出码语义（已实现，Task 7-0.10）**：

| mode | error 维 `partial` | error 维 `missing` | warn 维未满足 |
|------|---------------------|---------------------|----------------|
| `design` | 计 warning（**容忍**，不阻塞）| 计 error（**阻塞** exit 1）；若散文命中词表则降级为 `partial`→容忍 | warning |
| `seal` | 计 error（**阻塞**）| 计 error（**阻塞**）；启发式 OFF | warning |

- 词表：内置 6 维双语（EN+ZH）lexicon，扫 `title/context/goal`（及 `assumptions/constraints/non_goals`）散文；profile 可经可选 `heuristic_keywords` 字段**追加**词（additive，不改 core）。
- design 命中时：`status=partial`、`evidence=["heuristic:<kw>"]`、`suggestion` 提示「散文已提及，seal 前请结构化」。
- 闭合处置（resolved/waived/dynamic）见下，由 Task 7-0.11 落地；本 Task 仅落两阶段判定本身。

### 不确定性闭合（seal 阶段强制）

`mode=seal` 下，每个必填维必须处于以下三态之一，否则**不得 seal**（不变量 5 强化）：

| 处置 `disposition` | 含义 | 记录 |
|--------------------|------|------|
| `resolved` | 散文/启发式线索已**提升为结构化字段**，严格重查 `satisfied` | 结构化字段本身 |
| `waived` | 显式**登记忽略的不确定性**（可问责）| `{uncertainty_registry_ref, rationale, signer}` |
| `dynamic` | 绑定到**已注册的确定性 resolver 函数**，值运行时晚绑定但解析确定（「不确定动态确定」）| `{resolver_ref, schema}` |

**闭合不变式**：`sealed ⟹ ∀ 必填维 d: strict_satisfied(d) ∨ disposition(d) ∈ {waived, dynamic}`。
→ 落地时所有不确定性**要么被确定、要么被登记、要么被动态确定**，绝不静默放过。`waived` 与 `dynamic` 都进 sign-off 范围（见 Stage 7-2 §4c），可审计、有问责。

**不确定性 registry**：workspace 级（或 session 内）登记表，列出被接受/忽略的不确定性 + 理由 + 签结人 + 范围。让「忽略」从隐性变为显式可审计。

**dynamic resolver**：`resolver_ref` 指向一个**已注册的确定性函数**（同 `tm/policy/deterministic` 纪律），运行时计算该维的值。绑定本身结构化、可校验；禁止任意代码 / LLM 充当 resolver（守不变量 3）。

**已实现（Task 7-0.11）**——`tm/intent/uncertainty.py` + `compute_5w1h_completeness(..., dispositions=)`：

- resolver registry：`register_resolver` / `is_registered_resolver`；core 只内置领域中立的 `constant`（返回 `schema.value`），下游自注册。
- 校验（`validate_disposition`）：`waived` 必须带 `rationale`+`signer`；`dynamic` 的 `resolver_ref` 必须已注册；否则该维**不闭合**、`closure_reason` 点名原因 → seal 仍阻塞（绝不静默放过）。
- `resolved` 是**问责断言**：声明 resolved 但该维结构上仍未满足 → 拒绝闭合（"marked resolved but not structurally satisfied"）。
- seal report 额外字段：每维 `disposition` / `closed` / `closure_reason`；顶层 `sealed`（=可切结）、`closure`（dim→kind）、`summary.closed_by_disposition`。
- CLI：`--dispositions PATH`（JSON/YAML，dim→处置），仅 seal 消费。

---

## 4. Domain 5W1H Profile（可扩展性载体）

> **定位（重要）**：TraceMind 是**领域中立**的开源自治控制组件框架。**core 只内置 `base`（领域中立）profile**（`tm/intent/profiles/base.yaml`）。`fablenet` / `k8s` 等域 profile 是**下游/示例**，**不属于 TraceMind 本体**——它们由下游产品或用户自带，按**路径或下游搜索目录**加载（loader 已支持 path/base_dir 解析）。本仓库中它们只作为 **fixtures 用来校验「加新域 = 只加一个文件、core 零改动」**。

profile 是声明式文档，决定「哪些维必填、域特有要求、建议文案」。下游放在自己的目录（如 `<downstream>/profiles/<name>.yaml`），core 不收录。

```yaml
profile_id: fablenet.anonymity.v1
domain: fablenet
extends: base                 # 继承 base 默认 severity
severity_overrides:           # 覆盖维度 severity
  where: error                # 匿名域：必须声明作用域/拓扑
  when: error
required_slots:               # 域特有额外必填（进 slot_fills，不改 schema）
  - anonymity_scope
vocabulary_hints:             # 仅供 suggestion 模板，不参与判定
  where: "声明 AgentNetwork 拓扑或 IntentBody.metadata.domain=fablenet"
  when: "引用一个 liveness PropertyPattern 或挂带 triggers 的 Plan"
```

### 继承语义

- `base` profile 定义 §2 的默认 severity（Who/Why/What/How=error，When/Where=warn）。
- `extends: base` 起点继承 base，再被本 profile 的 `severity_overrides` 覆盖。
- `required_slots`：列出的 slot 名必须出现在 `IntentBody.slot_fills` 的某个 pattern 下，否则**降级 `required_slots_dimension` 指定的维**（默认 `what`）为 `partial`，并在 `missing_reason`/`suggestion` 点名缺失 slot；report 顶层附 `missing_required_slots`。（已实现 Task 7-0.4）
- `required_slots_dimension`（可选，默认 `what`）：声明 required_slots 缺失时降级哪一维（如匿名域可设 `where`）。随 `extends` 继承、可覆盖。
- **When 跨 artifact 取证**（已实现 Task 7-0.4）：除 Plan `triggers` / `when_*` slot 外，**引用的 PropertyPattern 若 `category=liveness`** 即算 When 证据（默认查 core seed 库；`--patterns DIR` 可指向下游模板库，best-effort、缺失不报错）。
- **新域 = 新增一个 profile 文件**；解析器（`load_profile`）按 `profile_id`/文件路径加载，代码零改动。

---

## 5. 输出（report，canonical JSON）

```json
{
  "profile": "fablenet.anonymity.v1",
  "mode": "design",
  "intent_id": "intent.anti_sybil.fairness",
  "dimensions": {
    "who":   {"status":"satisfied","severity":"error","evidence":["actors[2]"],"missing_reason":null,"suggestion":null},
    "why":   {"status":"satisfied","severity":"error","evidence":["context","goal"],"missing_reason":null,"suggestion":null},
    "what":  {"status":"satisfied","severity":"error","evidence":["goal","outputs[1]"],"missing_reason":null,"suggestion":null},
    "when":  {"status":"missing","severity":"error","evidence":[],"missing_reason":"no linked Plan and no temporal pattern referenced","suggestion":"引用一个 liveness PropertyPattern 或挂带 triggers 的 Plan"},
    "where": {"status":"satisfied","severity":"error","evidence":["metadata.domain=fablenet"],"missing_reason":null,"suggestion":null},
    "how":   {"status":"satisfied","severity":"error","evidence":["property_pattern_refs[2]"],"missing_reason":null,"suggestion":null}
  },
  "missing_dimensions": ["when"],
  "summary": {"total":6,"satisfied":5,"partial":0,"missing":1,"not_applicable":0,"errors":1,"warnings":0}
}
```

report 经 `tm.policy.deterministic.canonical_json_bytes` 归一化，重复跑 byte-identical（与 `tm/intent/coverage.py` 同风格）。

---

## 6. CLI

```
tm intents check-5w1h --intents PATH
                      [--profile NAME|PATH]   # 默认 base
                      [--mode design|seal]    # 默认 design（B 启发式）；seal=A 严格闭合
                      [--plan PATH]           # 取 When/How 证据
                      [--network PATH]        # 取 Where 证据
                      [--json]
```

`--mode seal` 时，report 额外含每维 `disposition`（resolved/waived/dynamic）与闭合校验结果；任一必填维未闭合 → exit 1。

- 人读 summary → stderr；canonical JSON → stdout（对齐 `tm intents coverage`）。
- exit code：0 = 无 error 维未满足；1 = 有。

---

## 7. 与既有校验的关系（互补，不重叠）

| 校验 | 关注 | 模块 |
|------|------|------|
| `validate_intent_tree` | intent **拓扑**（id/parent/root/cycle/leaf 有 success_criteria）| `tm/intent/tree_validator.py` |
| `compute_intents_coverage` | intent 是否被 **测试** 覆盖 | `tm/intent/coverage.py` |
| **`compute_5w1h_completeness`** | 单个 intent 的 **语义完整性**（5W1H 是否都描述了）| `tm/intent/completeness.py`（本契约，新增）|

三者正交：拓扑对、被测试覆盖、且 5W1H 完整 = 一个「结构上可落地」的需求。逻辑/语义正确性仍由 `tm verify`（CTL）裁决。

---

## 8. 不变量声明

- **确定性 / 零 LLM**：判定不调用任何 LLM；`completeness.py` 不得 import LLM 模块（CI 守）。
- **不落地**：`suggestion` 仅候选；补全/accept 必须人审（不变量 5）。
- **additive**：若需在 K-Ontology 登记 `metadata.domain` 等字段，走 v0.4 additive（遵 [`k-ontology-v0.3.md`](k-ontology-v0.3.md) §6 兼容纪律），不破 v0.3。
- **profile 治理**：domain profile 的 `severity_overrides`（尤其 `error`→`warn`/`off`）会削弱完整性门禁，因此 **profile 变更必须走 `ProposedChangePlanBody` 治理**（候选→verify→人审），不得本地静默改。守「spec 是经得起迭代考验的基石」。
- **完整性是相对的**：report 与 sign-off 必须显式标注「相对哪个 profile / 哪些已声明性质」算完整，**不得被读作绝对完整**。

---

## 9. References

- Stage 7-0 计划：[`../../../.plan/phase-7-stage-7-0-formal-language-5w1h.md`](../../../.plan/phase-7-stage-7-0-formal-language-5w1h.md)
- 现有 intent 校验：`tm/intent/tree_validator.py` · `tm/intent/coverage.py`
- IntentBody schema：`tm/artifacts/models.py`（`class IntentBody`）
- 设计循环 step 枚举（共用）：[`../../../.plan/phase-7-stage-7-2-intent-session.md`](../../../.plan/phase-7-stage-7-2-intent-session.md) §3
