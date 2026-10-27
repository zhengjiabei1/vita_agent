# 完整 dev 对比（11 用户 × 3 臂 × 1 trial）：真实结果

日期：2026-10-27。固定源码 `9632aa0`、模型 `deepseek-v4.1-flash`、`max_steps=50`、seed 42、每臂 1 trial。
数据：官方 personalization 划分中的 11 个 dev 用户，每人前 2 个子任务（共 22 个子任务）。
这是**开发集诊断**，不是保留测试集成绩。

原始产物：`artifacts/go-dev-full-v2/`（plan、三臂 JSON、日志、comparisons.json、analysis.json）。
复现：`python reports/2026-10-27-go-dev-full/analyze.py artifacts/go-dev-full-v2/plan.json`

## 主结果

| 用户 | 子任务领域 | baseline | harness | memory_harness |
| --- | --- | ---: | ---: | ---: |
| B471882 | ota, delivery | 0.0 | 0.0 | **1.0** |
| E057330 | delivery, delivery | **0.5** | 0.0 | **0.5** |
| K585155 | instore, instore | **0.5** | 0.0 | 0.0 |
| L725289 | delivery, ota | 0.0 | **1.0** | 0.0 |
| U011408 | instore, delivery | **0.5** | **0.5** | **0.5** |
| U602549 | delivery, instore | **1.0** | **0.5** | 0.0 |
| U804032 | delivery, instore | **0.5** | 0.0 | 0.0 |
| U900122 | delivery, ota | **0.5** | **0.5** | 0.0 |
| V162854 | ota, delivery | 0.0 | **0.5** | 0.0 |
| X193757 | ota, instore | **0.5** | 0.0 | 0.0 |
| Y481422 | delivery, delivery | **0.5** | 0.0 | 0.0 |
| **均值** | | **0.409** | **0.273** | **0.182** |

配对差值（用户级 bootstrap，10,000 次）：

| 对比 | 差值 | 95% CI |
| --- | ---: | --- |
| harness − baseline | −0.136 | [−0.409, +0.182] |
| memory_harness − baseline | −0.227 | [−0.500, +0.045] |

两个区间都包含 0。**没有证据表明任一模块带来提升；点估计反而更低。**

## 子任务领域（描述性，未做区间）

| 领域 | baseline | harness | memory_harness |
| --- | ---: | ---: | ---: |
| ota（5） | 0.0 | 0.4 | 0.2 |
| delivery（11） | 0.364 | 0.182 | 0.273 |
| instore（6） | 0.833 | 0.333 | 0.0 |

`update` 子集只有 3 对：baseline 1.0、harness 0.333、memory_harness 0.0；样本太小，不能解释为机制效应。

## 为什么不能据此说“harness 有害”

1. **每臂每用户只有 1 trial**。同一个用户在几乎相同的代码上，`memory_harness` 的分数已经出现过 0.5（v6）、0.0（v7/v8）、1.0（本轮）。单次轨迹的波动覆盖了臂间差异。
2. **改动同时改变了提示词**：guard 的完成前说明、候选列表都会改变模型措辞，进而改变用户模拟器的回复。本轮没有“只加 guard、不加提示”的隔离组。
3. **instore 的 0/6 不是被 guard 拦截**：抽查 U804032、X193757，订单/预约都成功执行，只是选错了店或商品（例如选了“早餐套餐券”，rubric 要求普通团购券；或把餐厅订到了桃园）。这是模型选择差异，不是执行前检查拒绝。
4. B471882 是此前唯一用于调参的用户，memory_harness 在本轮拿到 1.0；这更像过拟合风险，而不是泛化收益。

## 请求 / Token / 费用

共享 ledger `artifacts/go-dev-full-budget.sqlite3`：

| 指标 | 数值 |
| --- | ---: |
| 请求 | **928 / 1000** |
| 输入 Token | 13,835,361 |
| 输出 Token | 205,952 |
| 估算费用 | **2.19887535 USD** |
| 剩余请求 | 72 |

| variant | 请求 | 输入 | 输出 | 估算 USD |
| --- | ---: | ---: | ---: | ---: |
| baseline | 318 | 5,728,147 | 64,247 | 0.89777 |
| harness | 297 | 5,181,557 | 68,369 | 0.81825 |
| memory_harness | 313 | 2,925,657 | 73,336 | 0.48285 |

四轮累计 **1,546 请求**，估算约 **3.5128 USD**。

## 结论

- 这是一份完整的 dev 配对结果，但没有证明提升；点估计为负、区间跨 0。
- 当前最需要的是**多 trial 估计波动**，而不是继续加机制。若下一轮仍只在 1 trial 上比较，任何正负差异都不可信。
