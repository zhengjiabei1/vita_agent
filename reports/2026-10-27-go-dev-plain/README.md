# 提示词/约束隔离实验（8 用户 × 2 trials）：真实结果

日期：2026-10-28。固定源码 `65d3066`（429 分类修复在 `8b53774`）、模型 `deepseek-v4.1-flash`、50 步、seed 42。
数据：官方 dev 分区前 8 个用户，每人前 2 个子任务，每臂 2 trials。**开发集诊断，不是测试集成绩。**

原始产物：
- baseline：`artifacts/go-dev-plain-v1/baseline.json`（10 用户 × 2 trials，这里取前 8 用户）
- guards_plain：`artifacts/go-dev-plain-v2/guards_plain.json`（8 用户 × 2 trials）
- memory_guards_plain：`artifacts/go-dev-plain-v3/memory_guards_plain.json` + v2 中已完成的 `E057330`、`K585155`（合并为 8 用户 × 2 trials）

复现：
```bash
python reports/2026-10-27-go-dev-plain/analyze_split.py \
  --baseline artifacts/go-dev-plain-v1/baseline.json \
  --plan artifacts/go-dev-plain-v2/plan.json \
  --dataset artifacts/prefix2/personalization/tasks.json \
  --arm memory_guards_plain=artifacts/go-dev-plain-merged/memory_guards_plain.json
```

## 主结果

| 用户 | baseline | guards_plain | memory_guards_plain |
| --- | ---: | ---: | ---: |
| B471882 | 0.00 | 0.25 | 0.75 |
| E057330 | 0.50 | 0.50 | 0.25 |
| K585155 | 0.00 | 0.00 | 0.00 |
| L725289 | 0.25 | 0.25 | 0.00 |
| U011408 | 0.75 | 0.75 | 0.75 |
| U602549 | 0.50 | 0.25 | 0.50 |
| U804032 | 0.00 | 0.00 | 0.00 |
| U900122 | 0.50 | 0.25 | 0.00 |
| **均值** | **0.312** | **0.281** | **0.281** |

配对差值（用户级 bootstrap，10,000 次）：

| 对比 | 差值 | 95% CI |
| --- | ---: | --- |
| guards_plain − baseline | −0.031 | [−0.125, +0.062] |
| memory_guards_plain − baseline | −0.031 | [−0.250, +0.219] |

两个区间都跨 0，且点估计几乎为 0。**在这个 8 用户、2 trials 的队列上，只加约束检查、以及记忆+检查，都与 baseline 没有可检测差异。**

## 领域（描述性）

| 领域 | baseline | guards_plain | memory_guards_plain |
| --- | ---: | ---: | ---: |
| ota（6） | 0.167 | 0.167 | 0.167 |
| delivery（16） | 0.313 | 0.375 | 0.313 |
| instore（10） | 0.400 | 0.200 | 0.300 |

## 与上一轮的关系

上一轮 11 用户 × 1 trial：baseline 0.409、harness 0.273、memory_harness 0.182，点估计为负但区间跨 0。

本轮把 `harness` 中的提示词（完成前说明、候选列表）关掉，得到 `guards_plain` / `memory_guards_plain`：
- 负值没有复现，两个臂都回到 baseline 附近；
- 说明上一轮的负点估计**很可能来自单 trail 波动和/或提示词**，而不是约束检查本身；
- 但本轮没有在同一 2-trial 队列上重跑 `harness`，因此**不能**把差异严格归因于提示词。这需要下一轮把 `harness` 作为第四臂放进同一队列。

## 过程中遇到并修复的问题

1. **供应商连接中断**（2026-10-27）：`guards_plain` 前 3 条后出现 `APIConnectionError`，17 条失败，浪费约 145 请求。执行器正确拒绝不完整队列。
2. **429 误判为额度耗尽**（2026-10-28）：供应商返回 `rate_limit_exceeded (requests per second)`，`budget.py` 把所有 429 都永久 halt，导致 10 条轨迹被拒绝。已修复：普通 429 交给上游有界重试，只有 402/明确 quota 消息才 halt；新增回归测试。
3. 临时目录被系统清理，prefix2 数据集已按 HANDOFF 记录重建，SHA-256 与历史值 `c76985f0…c523fe` 完全一致。

## 请求 / Token / 费用

本轮（隔离实验）两个账本合计：

| 账本 | 请求 | 估算 USD | 状态 |
| --- | ---: | ---: | --- |
| `go-dev-plain-budget.sqlite3` | 1,351 / 1,850 | 3.28601 | 因 429 误判 halt，剩余 499 不可用 |
| `go-dev-plain-resume-budget.sqlite3` | 327 / 499 | 0.47950 | 完成 |
| **合计** | **1,678** | **3.76551** | 仍在授权 1,850 / 6 USD 内 |

另有 3 次供应商预检请求（独立账本，约 0.007 USD）。

项目四轮累计（不含预检）：**3,224 请求，估算约 7.2783 USD**。

## 结论

- 在 8 用户 × 2 trials 上，`guards_plain` 和 `memory_guards_plain` 与 baseline 无显著差异；点估计 −0.03，区间跨 0。
- 当前**没有任何真实证据支持“提升”**；也不支持“约束检查有害”。
- 下一步若要继续，应固定源码，把 `baseline / guards_plain / harness / memory_guards_plain` 放进同一多 trial 队列，直接估计提示词效应；仅凭现有跨队列比较不能下因果结论。
