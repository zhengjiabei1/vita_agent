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
- 针对真实 0 分完成一轮代码修正：官方 CGL 未知事实 fail-open、明确违反仍拦截；补充酒店品牌/电话叫醒/设施事实检查、未支付订单完成拦截和创建订单后自动支付；预算按 variant 归因；新增回归测试。
- 在 1 个 dev 用户 × 2 子任务上完成两轮 P0 smoke：baseline/harness/memory/memory_harness 均 0.0。第二轮 memory_harness 选中精确目标商品“清汤面”，地址、25 分钟、非方便面、支付均满足；但该子任务触发 `max_steps`，官方 evaluator 对过早终止直接返回 0、不调用 judge。已确认全部零分都来自真实 rubric 未满足或 `--max-steps 20` 过早终止，并已强制 `max_steps >= 50`、加入候选反馈。未声称提升。
- 完成第三轮 50 步 dev smoke（1 用户 × 2 子任务 × 1 trial，三个代码状态）：最高 **0.5**，其中两单为精确目标商品、地址正确、25 分钟、非方便面、已支付；但 baseline 同样拿到 0.5，且 n=1、代码不同，不能归因于任一模块，**未声称提升**。
- 292 项测试通过；本地演示与官方固定版本/数据哈希校验通过；三轮 Go 实验累计 618 请求、估算约 1.3139 USD；正式测试集评测和 skills/full 五组消融尚未完成（额度不足，需新计划）。

**可诚实写的能力关键词**：VitaBench 2.0、LLM Agent、执行前约束检查、结构化偏好记忆、证据引用、离线技能演化、可复现实验、配额/预算控制、Python、pytest。

## English version

**Training-Free Personalized Service Agent on VitaBench 2.0 (research prototype)**

- Integrated the official VitaBench 2.0 Agent / Memory / Evaluator interfaces at pinned commit `f60169e`, with a leakage-safe user split of 56 users and 771 subtasks (train 33/464, dev 11/149, test 12/158) and pinned dataset SHA-256.
- Built a draft → check → regenerate execution harness that validates budget, allergens, dates, quantities, inventory, account ownership and idempotency against trusted tool observations across 30 official write tools; rejected drafts are never executed.
- Implemented structured preference memory with exact evidence quotes and supersession chains, separating hard constraints from soft preferences, plus optional LLM extraction and raw-evidence fallback retrieval.
- Implemented offline skill distillation from failed training traces, development-set utility retirement and SHA-256 freezing; citation-checked v2 reflection snapshots load through the same read-only bank used by experiment plans.
- Implemented a shared quota ledger for currency, free request counts and provider credits; free mode rejects fabricated zero prices and halts immediately on 402/429/quota exhaustion across train/dev/test/reflection.
- Ran real training-prefix diagnostics (1 user × 2 subtasks): DeepSeek baseline/harness and OpenCode Go `deepseek-v4.1-flash` baseline/harness/memory all scored 0.0; identified high-GI meal selection, wrong hotel brand, and a memory brand change that still missed service/payment constraints, with no evaluator-defect evidence. **No performance improvement is claimed.**
- Followed the real zero-score diagnosis with a code-only correction pass: fail-open CGL for unknown facts, explicit violations still blocked, hotel brand/phone-wakeup/feature facts, unpaid-order completion guard, automatic payment after order creation, and per-variant budget attribution, with regression tests.
- Ran two P0 dev-smoke rounds on 1 user × 2 subtasks: baseline/harness/memory/memory_harness all scored 0.0. In the second round memory_harness selected the exact target item “清汤面” with the correct address, 25-minute delivery, non-instant-noodle tag and paid status, but that subtask hit `max_steps`; upstream returns 0 without invoking the judge for premature termination. Every zero is accounted for by a genuinely unmet rubric or the `--max-steps 20` cap; plans now enforce `max_steps >= 50` and blocked writes receive observed-candidate feedback. No improvement is claimed.
- Ran a third 50-step dev smoke (1 user × 2 subtasks × 1 trial across three code states): the best score was **0.5**, with two orders matching the exact target item, correct address, 25-minute delivery, no instant-noodle tag and paid status; baseline also scored 0.5, so with n=1 and differing code states this cannot be attributed to any module and **no improvement is claimed**.
- 292 tests pass; pinned version/data checks and local demo pass; the three Go rounds used 618 requests (~1.3139 USD estimate); formal held-out evaluation and the skills/full five-variant ablation remain unfinished (quota exhausted, new plan required).

## 不应写的内容

- “在 VitaBench 2.0 上提升 13 个百分点 / 达到 0.570 / 成本降低 8.6% / 偏好准确率 0.38→0.48 / 提升 4.6pp”。
- “完成 5 组消融并证明记忆/技能有效”。
- “在测试集上取得 X 分”。
- “使用 OpenCode Go 完成了正式五组/测试集评测并证明提升”（只完成了 1 个训练用户、2 子任务的三臂诊断，均为 0.0）。

如果未来完成正式实验，必须用新模型重跑 baseline，并按 `docs/experiments.md` 的计划、真实均值、update 子集、用户级置信区间、请求/Token/费用/积分更新本文件。
