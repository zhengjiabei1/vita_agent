# 简历表述（只使用已核实的真实结果）

本文件用于避免把猜测值写进简历。以下表述都可以由仓库中的代码、测试和报告复核。**不要写 `0.440 → 0.570`、`+8.6%`、`0.38 → 0.48`、`4.6pp` 或任何正式测试集提升**，因为这些数字没有实验支持。

## 中文版本

**Training-Free Personalized Service Agent on VitaBench 2.0（研究原型）**

- 在固定官方 commit `f60169e` 上接入 VitaBench 2.0 的 Agent / Memory / Evaluator 接口，按用户划分 56 用户、771 子任务（train 33/464、dev 11/149、test 12/158），固定数据 SHA-256，避免测试集泄漏。
- 实现执行前 draft → check → regenerate：基于可信工具观察检查预算、过敏、日期、数量、库存、账户归属和幂等性，覆盖 30 个官方写工具；检查失败不执行、不伪造事实。
- 实现带原文证据和覆盖链的结构化偏好记忆，区分硬约束与软偏好；支持确定性规则、可选 LLM 抽取和原始证据回退检索。
- 实现从训练失败轨迹离线提炼技能、开发集效用淘汰和 SHA-256 冻结；LLM 反思 v2 快照已接入统一只读加载器和实验计划校验。
- 实现共享额度账本：支持货币、免费请求数和供应商积分三种模式；免费模式拒绝 0 单价伪造，遇到 402/429/额度耗尽立即 halt，训练/开发/测试/反思共用上限。
- 完成真实训练集短前缀诊断：1 个训练用户 × 2 子任务；旧 DeepSeek 的 baseline/harness 与 OpenCode Go `deepseek-v4.1-flash` 的 baseline/harness/memory 均为 **0.0**；定位到高 GI 外卖、错误酒店品牌、memory 品牌改善但服务/支付约束未满足，未发现评分器缺陷。**未声称性能提升。**
- 268 项测试通过；本地演示与官方固定版本/数据哈希校验通过；Go 三臂用 176/200 请求、估算 0.3084 USD；正式测试集评测和 skills/full 五组消融尚未完成（额度不足，需新计划）。

**可诚实写的能力关键词**：VitaBench 2.0、LLM Agent、执行前约束检查、结构化偏好记忆、证据引用、离线技能演化、可复现实验、配额/预算控制、Python、pytest。

## English version

**Training-Free Personalized Service Agent on VitaBench 2.0 (research prototype)**

- Integrated the official VitaBench 2.0 Agent / Memory / Evaluator interfaces at pinned commit `f60169e`, with a leakage-safe user split of 56 users and 771 subtasks (train 33/464, dev 11/149, test 12/158) and pinned dataset SHA-256.
- Built a draft → check → regenerate execution harness that validates budget, allergens, dates, quantities, inventory, account ownership and idempotency against trusted tool observations across 30 official write tools; rejected drafts are never executed.
- Implemented structured preference memory with exact evidence quotes and supersession chains, separating hard constraints from soft preferences, plus optional LLM extraction and raw-evidence fallback retrieval.
- Implemented offline skill distillation from failed training traces, development-set utility retirement and SHA-256 freezing; citation-checked v2 reflection snapshots load through the same read-only bank used by experiment plans.
- Implemented a shared quota ledger for currency, free request counts and provider credits; free mode rejects fabricated zero prices and halts immediately on 402/429/quota exhaustion across train/dev/test/reflection.
- Ran real training-prefix diagnostics (1 user × 2 subtasks): DeepSeek baseline/harness and OpenCode Go `deepseek-v4.1-flash` baseline/harness/memory all scored 0.0; identified high-GI meal selection, wrong hotel brand, and a memory brand change that still missed service/payment constraints, with no evaluator-defect evidence. **No performance improvement is claimed.**
- 268 tests pass; pinned version/data checks and local demo pass; the Go three-arm run used 176/200 requests (~0.3084 USD estimate); formal held-out evaluation and the skills/full five-variant ablation remain unfinished (quota exhausted, new plan required).

## 不应写的内容

- “在 VitaBench 2.0 上提升 13 个百分点 / 达到 0.570 / 成本降低 8.6% / 偏好准确率 0.38→0.48 / 提升 4.6pp”。
- “完成 5 组消融并证明记忆/技能有效”。
- “在测试集上取得 X 分”。
- “使用 OpenCode Go 完成了正式五组/测试集评测并证明提升”（只完成了 1 个训练用户、2 子任务的三臂诊断，均为 0.0）。

如果未来完成正式实验，必须用新模型重跑 baseline，并按 `docs/experiments.md` 的计划、真实均值、update 子集、用户级置信区间、请求/Token/费用/积分更新本文件。
