# OpenCode Go dev smoke（P0 修正后）

日期：2026-10-27。模型：`deepseek-v4.1-flash`（OpenCode Go）。开发集 1 个用户 × 前 2 个子任务 × baseline/harness/memory 各 1 trial。**这是 dev 诊断，不是保留测试集结果。**

## 真实分数

| 实验臂 | 子任务 0 | 子任务 1 | 平均分 | 终止原因 |
| --- | ---: | ---: | ---: | --- |
| baseline | 0.0 | 0.0 | **0.0** | `agent_stop` |
| harness | 0.0 | 0.0 | **0.0** | `agent_stop` |
| memory | 0.0 | 0.0 | **0.0** | `user_stop` |

三臂仍全部 0 分。没有提升，没有置信区间（1 个用户）。

## 可观察行为

`summary.json` 只使用 user hash，不含地址、姓名、订单号或模型原文。

| 实验臂 | 酒店 | 外卖 |
| --- | --- | --- |
| baseline | 选到“经济大床房(带浴缸)”，价格 189；**未支付** | 清汤方便拉面；已支付 |
| harness | 同样选到带浴缸房型；**未支付** | 清汤方便拉面；已支付 |
| memory | 精选大床房（**无浴缸**）；**未支付** | 清汤排骨面（非方便面）；已支付，但配送 26 分钟 |

- 三臂工具错误均为 0。
- 该 dev 用户的酒店 rubric 要求经济型、连锁、2025-02-11、带浴缸、停车场、位于重庆。baseline/harness 选到了带浴缸房型，但没有支付；memory 没选到带浴缸。
- 外卖 rubric 要求清汤面、诊所地址、25 分钟内、非方便面。baseline/harness 选了方便面；memory 非方便面但 26 分钟，超 1 分钟。

## 请求、Token、费用

共享 ledger 硬上限 200 请求 / 1.00 USD：

| variant | 请求 | 输入 Token | 输出 Token | 估算 USD |
| --- | ---: | ---: | ---: | ---: |
| baseline | 55 | 900,005 | 11,597 | 0.14196 |
| harness | 56 | 896,053 | 12,391 | 0.14184 |
| memory | 75 | 443,823 | 21,553 | 0.07951 |
| **合计** | **186** | **2,239,881** | **45,541** | **0.36331** |

- 剩余 14 请求，已停止，没有继续跑 skills/full/test。
- 费用是按配置的 OpenCode 模型列表价格估算，不是供应商发票。
- memory 使用 `--memory-extraction rules`，没有额外 LLM 抽取请求；75 次主要是多轮对话/重生成。

## P0 修正后的观察

- CGL fail-open 后，baseline/harness 没有再因为“未知事实”反复阻塞到 `max_steps`；三臂都正常 `agent_stop`/`user_stop`。
- baseline/harness 选到带浴缸房型，说明上下文中的历史偏好能被模型使用；但支付仍未完成。
- 支付失败的原因是用户模拟器把“支付吧 + 支付成功 + ###STOP###”合并在一条消息里，agent 没有机会在 STOP 前调用支付工具。仅靠“用户授权后自动支付”太晚；后续代码已改为**观察到 create 订单后立即由 guard 支付**，但本轮剩余 14 请求不足以验证。
- memory 臂未选到带浴缸，说明 `hotel_features` 单槽会把“经济型/连锁/浴缸/停车”互相 supersede。后续代码已拆成 `hotel_feature.economy` 等独立槽位，也未在本轮验证。

## 第二轮（round2.json）

新增 `memory_harness`（结构化记忆 + guards，无技能），并修正自动支付、独立酒店设施槽、软偏好不得覆盖硬约束、方便面/配送时间规则。结果仍全部 **0.0**：

- v4 baseline/harness/memory_harness 均 0.0；`memory_harness` 酒店选到带浴缸并支付，外卖“清汤面”已支付但标签含方便面。
- v4 baseline 的外卖最终为“清汤面”、`shipping_time=25.0`、地址正确、非方便面、已支付，但 subtask 1 仍为 0.0。
- v5 `memory_harness`（加入软/硬槽和防覆盖修正）在酒店阶段 `max_steps`，没有酒店订单；外卖选中**精确目标商品** `清汤面`（P00083），地址正确、`shipping_time=25.0`、非方便面、已支付，但 subtask 1 仍为 0.0。

这组证据说明：**至少有一个子任务的最终状态看起来满足所有 rubric，但 trajectory judge 仍给 0 分**。下一步不应继续盲目加约束，而应先检查 trajectory judge 是否看到了足够证据（agent 最终回复是否逐条说明商品、地址、时间、非方便面），并可用 outcome evaluator 做离线诊断。另一种失败是硬设施约束无候选反馈导致 `max_steps`。

第二轮 ledger：176/200 请求，输入 2,379,714、输出 35,668，估算 0.37836 USD，剩余 24。两轮累计 362/400 请求，估算约 0.7417 USD。

## 边界

- 1 个 dev 用户、2 个子任务、1 trial；不能据此判断模块有效或无效。
- 没有 skills/full；没有训练失败反思；没有测试集。
- 三臂 0 分存在 floor effect，baseline 自己也是 0，无法测出模块增量。
- “状态满足但 judge 0”不等同于评分器 bug；也可能是 agent 回复没有给 judge 明确证据，必须进一步诊断。
- 旧计划已因后续源码变化失效；任何复跑必须新建计划。
