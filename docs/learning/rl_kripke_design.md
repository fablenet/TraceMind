### MDP 定义（MUST, Frozen @ v0）

- 时间步（step）MUST = 一次 controller cycle：Observe → Decide → Act。
  - 输入 MUST 为 `EnvSnapshot_t@v0`（canonical artifact）。
  - 动作 MUST 为 `ProposedChangePlan_t@v0`（canonical artifact）。
  - 反馈 MUST 为 `ExecutionReport_t@v0`（canonical artifact）。
  - 下一观察 MUST 为 `EnvSnapshot_{t+1}@v0`。

- 状态 `s_t` MUST = `feature(EnvSnapshot_t, previous_report_summary_{t-1})`。
  - `previous_report_summary_{t-1}` MUST 由 `ExecutionReport_{t-1}` 经 `ReportSummarySpec@v0` 确定性生成；
    当 t=0 无上一步 report 时 MUST 为显式空结构。

- 奖励 `r_t` MUST 由 `ExecutionReport_t` 经 `RewardSpec@v0` 确定性计算。

- Episode MUST 为 continuing task；终止仅允许由 Run 结束或 `ExecutionReport_t.status` 中的确定性 fatal/abort 枚举触发。
  - 训练可使用 truncation rollout（默认 K=20 步），但 truncation MUST NOT 视为 terminal。

- 折扣因子 γ MUST 固定为 0.99（MDPSpec@v0）。修改 γ 必须版本化并进入 Candidate→Accepted 治理与回归评估。

- 数据来源 v0 MUST 为 offline dataset（从历史 run 构造 transitions）。在线采样/在线学习不得进入 v0 生产路径，需新版本治理。
