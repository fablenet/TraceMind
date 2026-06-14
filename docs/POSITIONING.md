# 定位简报：TraceMind 与 LLM 的关系

> 类型：定位简报（briefing）· 起草 2026-06-14
> 目的：固定产品定位，避免协作时（含 AI 协作者）把 TraceMind 跑偏成"LLM 产品"。
> 适用：TraceMind（开源确定性内核）及其在 FableNet 中的应用。

---

## TL;DR

**TraceMind 是一个确定性的自治控制系统设计/验证内核。LLM 是它一等重要的*设计副驾*——但收敛性、判决、信任由形式逻辑与人担责保证，不由 LLM 保证。我们*消费/编排* LLM，不*生产* LLM。**

---

## 1. 一句话定位

- **TraceMind**：帮助任何人做"自治控制组件"的设计、验证、运行的开源组件。核心是确定性的 Kripke + CTL 验证、5W1H 完备性、组合式验证。
- **LLM**：在 NL→形式化的迭代中加速收敛的协作工具，重要但**可降级、永不进判决路径**。
- **FableNet**：TraceMind 的一个真实、对抗性的应用环境（不是 TraceMind 本身）。

## 2. TraceMind 是什么 / 不是什么

| 是 | 不是 |
|----|------|
| 确定性验证/控制内核（零 LLM 关键路径） | LLM 产品 / 模型训练框架 |
| 编排、消费现成 LLM 作为设计副驾 | 自研、微调、托管大模型 |
| 形式逻辑保证"可收敛、可担责" | 用 LLM 输出当"真相"或判决依据 |

## 3. LLM 的角色：重要，但被形式逻辑套上缰绳

LLM **很重要**——它是把模糊需求快速推向形式化的关键工具。纪律在于：**收敛由形式逻辑判定，不由 LLM 自证。**

- **小步快跑（已落地于 `IntentSession`）**：每个 turn 只做一小步（澄清 / 补全 / `ai.refine_pattern`），不让 LLM 一口气吐大方案。
- **每步向形式化收敛**：每步过确定性门禁（5W1H 完备性 / CTL 非矛盾 / 去重）；soft warning 触发 `clarify`。
- **可回溯、可担责**：hash 链 journal + 人 `sign_off` + `seal` 闭合所有不确定性（resolved / waived / dynamic）。
- **可降级**：`ai.refine_pattern` 等 LLM 路径必须能退化到 fake/规则路径；少了 LLM，系统照常前进、判决不变（不变量 2）。

> 结论：LLM 越强越有用，但它再强也改不了"是否收敛"的判定，也伪造不了判决或审计链。

## 4. FableNet 与对抗环境：深度用 LLM，但不信任 LLM

FableNet 是真实、残酷的交互环境：会有协同带风向、叙事操纵、甚至来自机构层面的攻击（参见 anti-sybil / 网军 issue：Vector Diversity Penalty、匿名二次方投票、贝叶斯信任衰减等）。

- **必须深度用 LLM / 推理模型**：做语义级检测——协同意图识别、叙事操纵识别、观点多样性评估。
- **绝不能信任 LLM 输出为真相**：对手同样能操纵/污染模型。
- **两者不矛盾**：正因为环境对抗、LLM 可被操纵，"LLM 可替换 + 永不进判决路径 + 判决由确定性机制 + 人担责"这条纪律，**不是限制 LLM，而是让我们能安全地、在对抗前线深度地用 LLM**——某个模型被带偏或被攻击，也改不了判决、改不了审计链。

## 5. 不变量（由机器强制，不只是口号）

1. **K-plane 是唯一 verifier**：判决来自确定性 Kripke + CTL，可在 CI 复现。
2. **零-LLM 关键路径**：`scripts/check_no_llm_in_completeness.py` 用 AST 静态分析**禁止** LLM import 进确定性核心模块。
3. **LLM 可替换 / 可降级**：所有 LLM 路径有 fake/规则回落；LLM 永不为"进展"所必需。
4. **人担责**：需求最终由人的 `sign_off` 切结；工具不担责。

## 6. 对工程与采购决策的含义

- **不造模型**：不投入训练/微调/托管大模型的工程。
- **消费 / 编排**：通过现成 LLM API（或可选的本地推理）接入副驾；接谁、接不接都可替换。
- **算力**：TraceMind 自身负载是 CPU + 单线程确定性（吃单核性能与内存，不吃 GPU）。本地训练大模型不在路线上，因此**不以"训模型"为由配置 GPU**；若 FableNet 侧需要重推理，优先走 API 或独立推理节点，与确定性内核解耦。

---

## 参考

- 不变量与组合式验证：[`docs/verify/COMPOSITIONAL.md`](verify/COMPOSITIONAL.md)
- 设计回路 / IntentSession：`tm/intent/session.py`、`tm/intent/design_loop.py`
- 零-LLM CI 守卫：`scripts/check_no_llm_in_completeness.py`
- 需求来源：`orchestrator-core/docs/extra_requirements_of_phase7.md`
