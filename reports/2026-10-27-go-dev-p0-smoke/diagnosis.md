# Dev smoke 第二轮诊断：为什么会“状态满足却 0 分”

日期：2026-10-27。诊断只读取已有产物和固定上游源码，**没有调用任何模型，没有消耗请求**。

## 结论

此前的判断“至少有一个子任务状态满足所有 rubric，但 trajectory judge 仍给 0 分”**是错的**，已在所有文档中更正。

真实原因有两个，且都能从日志直接确认：

1. **过早终止惩罚**：`--max-steps 20` 让子任务在完成前被截断。官方 `src/vita/evaluator/evaluator.py` 对 `MAX_STEPS`、`TOO_MANY_ERRORS`、`INVALID_AGENT_MESSAGE` 直接返回 `reward=0.0`，**根本不调用 judge**。
2. **真实未满足 rubric**：其余被正常评分的轨迹确实选错（方便面、超时、房型无浴缸/无停车场）。

所以现有的 0.0 里没有“评分器漏看证据”的证据。

## 证据

官方固定 commit `f60169e` 的 `src/vita/evaluator/evaluator.py`：

```python
if simulation.termination_reason in {
    TerminationReason.TOO_MANY_ERRORS,
    TerminationReason.MAX_STEPS,
    TerminationReason.INVALID_AGENT_MESSAGE,
}:
    return RewardInfo(reward=0.0, info={"note": "Simulation terminated prematurely..."})
```

上游 README 的 personalization 示例使用 `--max-steps 50`（第 119 行），`experiments.build_plan` 的默认值也是 50；但本轮实际命令显式传了 `--max-steps 20`。

日志对应行：

- `artifacts/go-dev-p0-smoke-v4/baseline.log:273`：subtask 2 `reward=0.0`，`note: Simulation terminated prematurely ... MAX_STEPS`。该轨迹的外卖订单实际是目标商品 `P00083 清汤面`、地址正确、`shipping_time=25.0`、已支付。
- `artifacts/go-dev-p0-smoke-v5/memory_harness.log:162` 与 `274`：两个子任务都因 `MAX_STEPS` 直接判 0。v5 外卖同样是目标商品 `P00083 清汤面`、地址正确、`shipping_time=25.0`、非方便面、已支付。
- `artifacts/go-dev-p0-smoke-v4/harness.log:269`、`memory_harness.log:275`：正常 `sliding_window` 评分，`reward=0.0`。

## 逐个轨迹归因

| 轨迹 | 终止 | 最终状态 | 归因 |
| --- | --- | --- | --- |
| v4 baseline | `max_steps` | 外卖目标商品且已支付；酒店未支付、非目标房型 | 外卖因截断未评分；酒店真实失败 |
| v4 harness | `user_stop` | 酒店 `经济大床房`（无浴缸）；外卖 `日式清汤方便拉面`、26 分钟 | 真实未满足 rubric |
| v4 memory_harness | `user_stop` | 酒店 `经济大床房(带浴缸)`（该酒店无停车场）；外卖 `大厨清汤方便面` | 真实未满足 rubric |
| v5 memory_harness | `max_steps` | 外卖目标商品且已支付；酒店未成单 | 两个子任务都被截断，未评分 |

v5 酒店阶段被截断的机制：用户在对话里指定了没有停车场/浴缸的酒店，guard 正确 BLOCK，但 agent 只反复向用户确认，没有回到已搜索到的 `都市118连锁酒店(重庆大学城店)`（tags 含经济型/连锁/停车场/浴缸）。20 步很快耗尽。

## 修复（代码已改，未调用模型）

1. `experiments.MIN_MAX_STEPS = 50`：`build_plan` 拒绝 `max_steps < 50`，并给出“过早终止零分”的原因。旧的 20 步实验计划因此永远无法再被误用。
2. **BLOCK 候选反馈**：当写入因 `preference_conflict` 被拒时，guard 从已观察的搜索结果里列出满足全部硬约束的酒店/房型/商品，作为系统反馈附加到重生成消息。只使用读工具返回过的值，不引入新事实；下一次草稿仍要通过同一检查。
3. 修正文档中“judge 漏看证据”的错误结论。

离线回放验证（无模型调用）：把 v5 轨迹里的 `hotel_search_recommand` 结果喂给新解析器，43 家酒店全部解析；用经济型/连锁/浴缸/停车场四条硬约束过滤后得到 4 家，包含目标 `都市118连锁酒店(重庆大学城店)`（H00043）。旧轨迹里 agent 从未提到它，而是反复追问用户。

测试：288 项通过，`ruff --select E4,E7,E9,F` 通过。

## 下一步需要多少额度

- 单臂 `memory_harness`、2 子任务、50 步：预计 **60–110 请求**（20 步时单臂约 44–46 请求；候选反馈应减少空转）。
- 三臂 `baseline/harness/memory_harness`：预计 **200–330 请求**。

当前共享 ledger 只剩 24 请求，不够跑完任何一条 50 步轨迹，因此不能给出新的分数。需要新增额度后新建计划（源码已变，旧计划失效）。
