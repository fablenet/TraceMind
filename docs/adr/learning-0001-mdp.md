# ADR: learning-0001 — TraceMind RL MDP Definition (Frozen)

## Context
TraceMind 将引入 RL（Learning L0）。L0 硬要求：
1) 推理/执行必须可复现：一旦产生 ProposedChangePlan（PCP），replay 必须复用 plan，不得再次调用模型。
2) 关键产物必须能进入 Candidate→Accepted 治理：可 canonical/hashing、可审计、可回归。

为避免 RL “跑偏”或引入不可审计的隐式状态，需要冻结 MDP 定义：时间步/状态/终止/折扣 γ/数据来源。

## Decision (Frozen)
### Time step (t)
- **一个 step MUST 对应一次 controller cycle：Observe → Decide → Act。**
- Cycle 的输入输出边界 MUST 为：
  - 输入：`EnvSnapshot_t@v0`（Observe 输出，canonical artifact）
  - 动作：`ProposedChangePlan_t@v0`（Decide 输出，canonical artifact）
  - 环境反馈：`ExecutionReport_t@v0`（Act 执行结果，canonical artifact）
  - 下一观察：`EnvSnapshot_{t+1}@v0`

### State s_t
- **状态 MUST = feature(EnvSnapshot_t, previous_report_summary_{t-1})**
  - `EnvSnapshot_t`：必须是 canonical/hashing 的环境快照（结构稳定、可重放）。
  - `previous_report_summary_{t-1}`：必须由 `ExecutionReport_{t-1}` 通过一个 **确定性** 的 `ReportSummarySpec@v0` 生成。
  - 在 t=0（无上一步 report）时，`previous_report_summary` MUST 为空结构（显式 None/Null object），不得隐式填充。

### Action a_t
- **动作 MUST 定义为 `ProposedChangePlan_t@v0` 本体**（不是“模型内部 token/隐式策略”）。
- 训练数据中可以派生 action features，但派生 MUST 可复现且可追溯到 PCP 的内容 hash。

### Reward r_t
- **奖励 MUST 由 `ExecutionReport_t` 经 `RewardSpec@v0` 确定性计算得到。**
- RewardSpec 必须可版本化（进入 Candidate→Accepted），且 reward 计算不得依赖外部不可追溯信息。

### Episode / Terminal
- **任务类型 MUST 为 continuing task（理论上无限步）。**
- Episode 终止 MUST 只在“控制器停止/Run 结束”或“执行被标记为不可继续（fatal/abort）”时发生：
  - 终止条件 MUST 来自 `ExecutionReport_t.status` 的确定性枚举（例如 `FATAL`, `ABORTED`）。
- 训练时允许 **truncated rollout**：默认每段长度 `K=20` 步截断用于 batch/训练效率；
  - 截断 MUST NOT 视为环境终止（non-terminal truncation），bootstrap 规则由算法决定。

### Discount factor γ
- **γ MUST 固定为 0.99（MDPSpec@v0）。**
- 任何对 γ 的修改 MUST 走版本化治理（新的 MDPSpec 版本进入 Candidate→Accepted），并要求回归评估对比。

### Data source
- **v0 MUST 采用 offline dataset（从历史 run 构造 transitions）。**
- 在线采样/在线学习 MUST NOT 进入 v0 的生产路径；如引入，必须以显式 feature flag + 新的治理版本实现。

## Options Considered
1) Step = controller cycle（选中）
2) Step = 更细粒度（例如每个 tool 调用为一步）— 未选：边界复杂、状态爆炸、审计成本高。
3) State = EnvSnapshot only — 未选：无法表达“上一步执行反馈”，会迫使 policy 通过不可控记忆补偿。
4) Episode = 固定 horizon（例如 5 步一段）— 未选：会把“工程流程”伪装成短任务，导致 reward hacking / 局部最优。
5) γ = 0.95 — 未选：过度短视，容易牺牲可治理的长期质量指标（回归稳定性、修复成本）。
6) Data = online sampling first — 未选：不可控、不可复现实验干扰主线治理。

## Rationale
- controller cycle 是 TraceMind 的天然闭环边界：输入是可快照的环境，输出是可审计的 plan 与 report。
- 引入 previous_report_summary 是最小必要的“反馈记忆”，且通过 SummarySpec 固化为确定性派生，避免隐式 RNN/黑箱状态。
- continuing task + rollout 截断把“训练工程便利”和“MDP 语义正确性”解耦，防止把截断当终止引入偏差。
- γ=0.99 与长期质量指标一致（稳定性、回归、治理成本），且后续可通过版本化调整而不破坏可复现性。
- offline-first 保证 v0 可复现、可审计、可回归；在线采样以后必须显式纳入治理。

## Consequences
Positive:
- 所有 transition 的边界、状态来源、reward 计算均可追溯到 canonical artifacts（hash 链完整）。
- replay 不需要模型调用；训练/数据构造可在 workspace 快照上完全重放。
- 为后续 RL 算法替换留出空间（算法变，MDP 定义不变/版本化变）。

Negative / Trade-offs:
- 需要定义并维护 `ReportSummarySpec@v0` 与 `RewardSpec@v0`（但这是治理所需成本）。
- offline 数据可能分布偏移；在线采样需后续版本治理解决。

## Test Plan (No implementation required)
1) 文本样例检查：
   - 给出 `EnvSnapshot_t`, `ExecutionReport_{t-1}`, `ProposedChangePlan_t`, `ExecutionReport_t`, `EnvSnapshot_{t+1}`，
     验证能构造唯一的 (s,a,r,s')。
2) 确定性检查（规范级）：
   - `ReportSummarySpec@v0`：同一 ExecutionReport 输入 → summary 输出内容 hash 恒定。
   - `RewardSpec@v0`：同一 ExecutionReport 输入 → reward 数值恒定。
3) Dataset builder 骨架的可复现性检查（实现后）：
   - 同一 workspace 快照 → TransitionRecord 序列（含 hash）完全一致（排序稳定、无随机）。
