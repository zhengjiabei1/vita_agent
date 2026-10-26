# OpenCode Go 真实训练 prefix2 结果

日期：2026-10-27。模型：`deepseek-v4.1-flash`，提供方 OpenCode Go，端点 `opencode.ai`。这是**训练集 1 个用户 × 前 2 个子任务 × 每臂 1 trial** 的诊断，不是保留测试集成绩，也不能证明模块收益。

## 真实分数

| 实验臂 | 子任务 0 | 子任务 1（update） | 平均分 | 终止原因 |
| --- | ---: | ---: | ---: | --- |
| baseline | 0.0 | 0.0 | **0.0** | `max_steps` |
| harness | 0.0 | 0.0 | **0.0** | `max_steps` |
| memory | 0.0 | 0.0 | **0.0** | `agent_stop` |

- baseline vs harness：**Δ = 0.0**
- baseline vs memory：**Δ = 0.0**
- 只有 1 个用户，用户级 paired bootstrap 置信区间为 **null**，不能报告显著性。
- update 子集每臂均为 0.0。

## 可观察的约束/行为

`summary.json` 使用 task ID 哈希，不含地址、姓名、原始 prompt 或模型文本。

| 实验臂 | 外卖选择 | 酒店选择 | 其他 |
| --- | --- | --- | --- |
| baseline | 含 `高GI` 的牛肉盖饭 + 素菜；订单未支付 | JW 万豪；两晚；已支付 | 20 步耗尽，9 次工具调用，0 工具错误 |
| harness | 含 `高GI` 的牛肉盖饭 + 素菜；已支付 | JW 万豪；两晚；未支付 | 20 步耗尽，12 次工具调用，4 次校验/兜底阻塞，0 工具错误 |
| memory | 鱼肉套餐（不符合 rubric 接受的畜/禽肉）；已支付 | 喜来登（品牌正确）；两晚；未支付；缺少电话叫醒 | `agent_stop`，9 次工具调用，0 工具错误 |

结论：三个臂都 0 分。memory 把酒店品牌改成了喜来登，但仍未满足电话叫醒、支付、外卖肉类/低 GI 等条件；不能把品牌变化称为整体提升。baseline/harness 继续选择高 GI 外卖和错误酒店品牌，说明仅靠 full-context 或执行检查不足以解决偏好遵循。

## 请求、Token 和费用

本次 Go 实验使用共享 ledger，硬上限 **200 请求** 和 **1.00 USD**：

- 实际 **176 请求**，剩余 24 请求。
- 输入 **1,828,931** Token，输出 **56,745** Token，总计 **1,885,676** Token。
- 按配置的 OpenCode 模型列表价格（input 0.15、output 0.6 USD / 1M）估算 **0.30838665 USD**。
- 这是配置单价估算，**不是供应商发票**；供应商 `list_cost`、缓存折扣和实际扣款可能更低。
- 3 次成功的 provider 预检另用独立 ledger：106,996 输入、69 输出，估算 0.01609 USD。预检验证了工具调用、JSON、106,659 Token 长上下文和 usage 字段。

未再发起 skills/full 或测试集调用：剩余 24 请求不足以完成一个有效的 dev 技能筛选 + 五组消融，且还没有经真实训练失败验证的冻结技能库。

## 配置与复现

| 字段 | 值 |
| --- | --- |
| 数据 | prefix2，SHA-256 `c76985f042b0be3f751e7a97261e3fe70f6e96402bc6de7f358e025433c523fe` |
| 官方 commit | `f60169e89f30499cb7883f3dad76bd03facc908d` |
| seed / max_steps / trials | 42 / 20 / 1 |
| 并发 | 1 |
| 模型配置 SHA-256 | `64ea41d17541400e6bdec7992392734b219220173829fd8cd988936027156576` |
| baseline+harness 计划 SHA-256 | `84bcceb4a15bc2a8e1c00b3152e795f2c1f1571bb12c4b1f8aaacd1a0a6886f4` |
| memory 计划 SHA-256 | `a3d24066a4b789fe1b30f6b91608e30cbfc729deeca78c359d7073663ee5ba6d` |

baseline/harness 在“structured memory 当前轮抽取传递 llm/args”修复之前完成；该修复只影响 memory 臂，baseline/harness 使用官方 full-context memory，不会进入该代码路径。由于源码哈希随后变化，这两个历史计划不能再直接 execute；memory 计划是在修复后创建的。任何正式复跑都必须新建计划。

```bash
set -a; . ./.env; set +a
python scripts/run_experiments.py execute artifacts/go-train-prefix2-v1/plan.json \
  --budget-usd 1.0 --max-requests 200 --budget-ledger artifacts/go-shared-budget.sqlite3
```

`OPENCODE_API_KEY` 和 `OPENCODE_SESSION_ID` 只从 `.env`/环境读取，不进入 Git 或报告。

## 边界

- 不是测试集结果，不是五组消融，不是多 trial 稳定性结果。
- 三个 0 分不能说明“模块无效”，只能说明在这个用户/两个子任务上没有得分。
- 不能把 memory 的品牌变化单独归因于记忆模块；没有同用户重复 trial，也没有消融外的因果设计。
- 旧 DeepSeek baseline 与新 Go baseline 不可直接比较；换模型后已重跑同模型 baseline。
- 简历提升数字仍无实验支持。
