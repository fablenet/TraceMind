# Design Workbench API/CLI 契约 v0.1（Task 7-5.0）

**Version**: tracemind.io/workbench-api/v0.1
**Status**: 契约已冻结并**代码化**（2026-06-13）
**父计划**: [`../../../.plan/phase-7-stage-7-5-design-workbench.md`](../../../.plan/phase-7-stage-7-5-design-workbench.md)
**目的**: 把「设计循环」每一步显式成 **v1 API + 对应 CLI**，并给出 **Parity 清单**。本文档**只冻结契约**，不实现。它保证 Stage 7-0/7-1/7-2 落地时 API 天生支持有序编排，避免「先只做 CLI、回头表达不了流程」的返工。

> **代码化单一真相源（2026-06-13）**：本契约的步枚举 / 动作词表 / 门禁 / Parity 矩阵已落成 [`tm/intent/design_loop.py`](../../tm/intent/design_loop.py)（纯确定性、零 LLM），由 Stage 7-2（IntentSession 状态机）与 Stage 7-5（工作台 + Parity CI）共同 import。`tests/test_design_loop_contract.py` 冻结词表并机检 Parity Rule（每动作 API+CLI+等价性齐全）。**改本契约 = 改该模块**，非散落各处。

---

## 0. 双平面与 Parity Rule（重申）

- **自动化平面**：CLI + tm-server v1 API。原子、无状态、**无强制顺序**、可脚本、CI 友好。永久保留（不变量 2 兜底）。
- **交互平面**：设计工作台。在自动化平面之上叠加**有序流程 + 门禁**。
- **Parity Rule（铁律）**：工作台能做的每个动作，**必须**在本契约里有一条 API + 一条 CLI。工作台无独有能力。

---

## 1. v1 API 约定（沿用现状）

观察现有路由前缀：`/api/v1/{llm, network, controller, workspaces, runs, artifacts, meta}`（见 `tm/server/routes_*.py`）。

本契约**新增** 4 个资源前缀（additive，不动现有）：

| 前缀 | 资源 | 状态 |
|------|------|------|
| `/api/v1/intents` | Intent draft + 5W1H 完整性 | 新增 |
| `/api/v1/patterns` | propose / refine 候选 | 新增（propose 逻辑复用 `ai.propose_pattern_instances`）|
| `/api/v1/verify` | 形式验证（含 compositional）| 新增（复用 `network_verify`，已有 `/api/v1/network` 可并）|
| `/api/v1/sessions` | IntentSession 生命周期 + 当前步 | 新增（Stage 7-2 artifact）|

> `/api/v1/network` 已存在；`/api/v1/verify` 是否并入它，留 §8 决策。

---

## 2. 设计循环步骤契约（核心）

每步：API · CLI · 输入/输出 artifact · 性质（确定性 / LLM候选 / 人工门禁）· 现状。

| # | 步骤 | API | CLI | in → out | 性质 | 现状 |
|---|------|-----|-----|----------|------|------|
| 1 | Intent draft | `POST /api/v1/intents` `PUT /api/v1/intents/{id}` | `tm intents new` / 编辑文件 | NL/字段 → IntentBody | 人 + LLM候选 | 部分（schema 有，CLI new 待加）|
| 2 | **5W1H 完整性** | `POST /api/v1/intents/{id}:check-5w1h` | `tm intents check-5w1h` | Intent(+Plan/Network) → CompletenessReport | **确定性(7-0)** | 新增（7-0）|
| 2a | 补全建议（候选）| 同上响应内 `suggestions[]` | 同上 stdout | → 模板化建议 | 候选（非LLM模板）| 新增（7-0）|
| 2b | 二义澄清 `clarify` | `POST /api/v1/sessions/{id}:clarify` | `tm intent session clarify` | 软告警 → 处置(确认/合并/补需求/改约束) | 人主导 + 候选问题 | 新增（7-2 §4b）|
| 3 | propose 候选 | `POST /api/v1/patterns:propose` | `tm pattern propose`（现 `ai.propose_*`）| NL+library → PatternProposal[] | LLM候选/fake兜底 | 部分（fake 有，真LLM 7-1）|
| 3b | 多轮 refine | `POST /api/v1/patterns:refine`（带 session）| `tm intent chat` | proposal+反馈 → 新 proposal | LLM候选/fake兜底 | 新增（7-2）|
| 4 | 实例化 + 编译 | `POST /api/v1/patterns:instantiate` | `tm pattern instantiate` | proposal+slots → PatternInstance/Bundle | 确定性 | 已有 |
| 5 | **验证** | `POST /api/v1/verify:network` (`?mode=monolithic\|compositional`) | `tm verify network [--mode …]` | Network+bundles+formulas → VerifyReport | **确定性(7-V)** | 已有（compositional 7-V 加）|
| 6 | **人审 accept** | `POST /api/v1/artifacts/{id}:accept`（经 ProposedChangePlan）| `tm artifacts verify` / `tm proposal …` | 候选 → accepted artifact | **人工门禁(不变量5)** | 已有（治理基线）|
| 7 | **切结 seal** | `POST /api/v1/sessions/{id}:seal`（带 sign_off + 闭合 dispositions）| `tm intent session seal` | session → sealed；每必填维 resolved/waived/dynamic 闭合 | **人工签结(不变量5强化)** | 新增（7-2 §4c · 5w1h §3b）|

---

## 3. Session 生命周期（顺序/可续的承载）

工作台无自有状态；一切落 **IntentSession** artifact（Stage 7-2）。

| 动作 | API | CLI | 说明 |
|------|-----|-----|------|
| 新建会话 | `POST /api/v1/sessions` | `tm intent chat --new` | 绑定一个 root Intent |
| 读当前步 | `GET /api/v1/sessions/{id}` | `tm intent session show` | 返回 `current_step`（设计循环 1–6 哪一步）+ 完整度状态 |
| 推进 / 回退 | `POST /api/v1/sessions/{id}:advance` / `:revert` | `tm intent session advance/revert` | 仅改 session 指针；实际动作仍调步骤 API |
| 暂停 / 续 | session 是持久 artifact，无需显式暂停；任何时刻可 `GET` 续 | `tm intent session resume {id}` | 跨日 / git 接力（不变量：状态在 artifact 不在前端）|

**门禁（写进 advance 的前置条件，确定性判定）**：
- 步 2 完整性未过（有 error-severity 缺失）→ 拒绝 advance 到步 5。
- 步 5 verify 未过 → 拒绝 advance 到步 6。
- 步 6 accept **必须人工**触发，API 不接受「自动 accept」标志（不变量 5）。

---

## 4. Parity 矩阵（CI 守）

每行必须三列齐全；缺任一列即违反 Parity Rule，CI 失败。

| 工作台动作 | API | CLI | 等价性测试 |
|-----------|-----|-----|-----------|
| 起草 Intent | `POST /intents` | `tm intents new` | 产出 IntentBody byte-identical |
| 查 5W1H | `:check-5w1h` | `tm intents check-5w1h` | report canonical-json 相等 |
| 提候选 | `:propose` | `tm pattern propose` | candidates 相等（同 provider/seed）|
| 多轮 refine | `:refine` | `tm intent chat` | 同输入序列 → 同 proposal |
| 实例化 | `:instantiate` | `tm pattern instantiate` | PatternInstance byte-identical |
| 验证 | `:network` | `tm verify network` | VerifyReport verdict 相等 |
| accept | `:accept` | `tm proposal`/`tm artifacts verify` | accepted artifact 相等（人工门禁标注豁免自动化）|

> `test_workbench_cli_parity.py`（Stage 7-5.2）遍历本矩阵断言等价。accept 的「人工决策」环节标注为人工门禁，仅校验 accept *动作* 的 API/CLI 一致，不校验决策本身。

---

## 5. 顺序编排模型（回应「命令行无法控制先后」）

- **CLI / API 单条 = 原子、无序**：可任意顺序调用（自动化平面不变）。
- **顺序只存在于 Session**：`current_step` + `advance`/`revert` + 门禁，构成设计循环的**软状态机**。工作台读 session 决定「下一步推荐做什么、哪些被门禁挡住」。
- → 顺序是 session 上的一层编排，**不污染**原子 API/CLI。关掉工作台，纯 CLI 仍可按 session 提示手动跑完整循环。

---

## 6. 现状 vs 新增（落地缺口）

| 能力 | 现状 | 需新增 | 归属 Stage |
|------|------|--------|-----------|
| Intent schema | ✅ models.py | `tm intents new` + `POST /intents` | 7-5.1 / 7-2 |
| 5W1H 检查 | ❌ | 模块 + API + CLI | **7-0** |
| propose（fake）| ✅ ai.propose | API 包装 | 7-1 |
| propose（真LLM）/ refine | ❌ | provider + refine + session | 7-1 / 7-2 |
| instantiate / compile | ✅ | API 包装 | 7-5.1 |
| verify network | ✅ | compositional 模式 + API | **7-V** |
| accept / governance | ✅ proposal/gate | API 包装 | 7-5.1 |
| Session 生命周期 | ❌ | IntentSession artifact + `/sessions` | **7-2** |
| Parity CI | ❌ | parity 测试 | 7-5.2 |

→ 本契约把缺口对齐到各 Stage；**只要各 Stage 先交付「API + CLI」形态**，工作台（7-5.3+）即薄壳。

---

## 7. 不变量对账

| 不变量 | 契约如何守 |
|--------|-----------|
| 2 LLM/UI 可替换 | 每动作 API+CLI 齐全；propose 失败回落 fake；CLI 永久保留 |
| 3 K-plane 是 verifier | 步 2/5（完整性/验证）确定性；LLM 仅步 3/3b 提候选 |
| 5 AI 只产候选 | 步 6 accept 必须人工触发；API 无自动 accept |
| 6 拓扑离散性 | verify 复用 star；不引入新拓扑 |

---

## 8. 待实现期决策（Open）

1. `/api/v1/verify` 独立 vs 并入既有 `/api/v1/network`？（倾向：verify 为动词族，network 为资源；可 `/api/v1/network/{id}:verify`）
2. ~~IntentSession 是新 kind 还是借 Trace？~~ **已决（Stage 7-2）：新 kind `intent_session`，K-Ontology v0.4 additive**。见 [`../../../.plan/phase-7-stage-7-2-intent-session.md`](../../../.plan/phase-7-stage-7-2-intent-session.md) §2。
3. ~~`current_step` 枚举与门禁映射？~~ **已决（Stage 7-2）：step 枚举 `{draft, check_5w1h, propose, refine, verify, accept, sealed}` 由 7-2 与本契约共用，落 `tm/intent/`**。见 7-2 计划 §3。
4. accept 的 API 形态：直接 `:accept` vs 强制走 `ProposedChangePlanBody`（治理基线倾向后者）。
