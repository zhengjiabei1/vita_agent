# 可复现的评测约定

简历中的 `0.440 → 0.570`、`+8.6%` 成本、偏好更新 `0.38 → 0.48` 和技能单独收益 `4.6pp` **已由用户说明是猜测值，没有实验支持**。它们不是本项目成绩，也不是实验必须达到的目标；本地演示不能证明这些结果。

## 配对实验

保留相同的官方数据版本、Agent/User/Evaluator 模型及采样参数、随机种子、用户集合、trial 数量、最大步骤数、计分方式。建议分别运行 baseline、仅约束检查、仅结构化记忆、仅冻结技能库、全部模块五组。技能库收益应比较“仅冻结技能库”与 baseline；不能把全模块收益归因于技能库。

使用官方 `vita run --save-to ...` 生成的 JSON，随后调用：

```python
from vita_agent.evaluation import compare_runs
report = compare_runs("baseline.json", "candidate.json")
```

`compare_runs` 直接读取官方 `Results` 格式（`info`、`tasks`、`simulations`），不需要导入 VitaBench 或调用模型。它校验声明的全部 `tasks × trials`，拒绝缺失 trial、重复记录、未评分任务和缺失的 subtask reward，防止只保留成功样本。发生崩溃时应补跑，或在独立、可审计的转换流程中为失败的完整任务显式记零；本工具不会把缺失记录自动视为已完成评测。

主指标对每个用户等权：先平均该用户的 subtask，再平均 trials，最后平均用户。更新子集读取官方 `reward_info.info.subtask_skill_tested` 中的 `update` 标签，对匹配的 subtask/trial 进行微平均。它与用户等权主指标的权重不同，报告会明确标注。主指标同时提供按用户整体重采样的配对 bootstrap 95% 区间（默认 1000 次，种子 42）；只有一个用户时不返回区间。

工具检查输出中存在的模型、种子、步骤限制和官方代码提交。官方当前 `Info` 通常没有 Evaluator 模型配置，缺失字段列入 `settings_unverified`，不能据此声称所有实验条件已验证。仍需另存实验配置、数据校验和与 prompt/技能快照哈希。

## 成本

成本读取官方 `agent_cost`，任一记录缺失时总成本和增长率均返回 `null`。baseline 成本为零时增长率也为 `null`，不能称“零开销”。公式为 `(candidate_total / baseline_total - 1) × 100%`。

此字段仅覆盖上游记录的 Agent 调用。检查重生成过程中被丢弃的草稿、记忆抽取调用是否被计费日志完整记录；另外报告离线技能提炼成本、User 模型与 Evaluator 成本。无梯度训练不等于零推理费用。正式汇报 `+8.6%` 前，应说明采用的成本范围、定价表、请求数、token 用量与缺失值处理。

## 防止跨用户泄漏

```python
from vita_agent.evaluation import split_users
manifest = split_users(all_user_ids, seed=42)
```

以**用户**划分 train/dev/test，保持同一用户的全部交互、subtask 和 trial 在同一集合。仅从 train 失败轨迹提炼技能；dev 用于效用统计、淘汰和超参数选择；完成后冻结技能库并记录哈希。冻结时传入 test 用户对应的全部 `task_id`，以核对技能来源不含测试任务。测试期不得增删技能、更新效用或调节阈值。

允许记忆模块在某个测试用户自己的任务序列内按基准协议更新，但不能把该用户的信息写进跨用户的共享技能库。这两种状态应分别保存。
