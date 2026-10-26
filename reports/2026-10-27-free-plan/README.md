# 免费模型训练计划（未执行）

日期：2026-10-27。计划在本地 `artifacts/free-train-prefix2-final3/plan.json`（gitignored），**没有 execute**。原因是 OpenCode 免费额度返回 429，见 [provider quota](../2026-10-27-provider-quota/)。

## 固定配置

| 字段 | 值 |
| --- | --- |
| 官方 commit | `f60169e89f30499cb7883f3dad76bd03facc908d` |
| 数据 | 完整数据集每用户前 2 子任务 |
| 数据 SHA-256 | `c76985f042b0be3f751e7a97261e3fe70f6e96402bc6de7f358e025433c523fe` |
| 用户划分 | seed=42，train 33/464、dev 11/149、test 12/158（本计划只选 1 个训练用户） |
| 模型 | `deepseek-v4.1-flash-free` |
| billing | `free`，`free_quota.unit=request` |
| 模型配置 SHA-256 | `fbbbee7c40301513a6ae2b6306ea5b48388241a488114175a38da9de8477db30` |
| 实验计划 SHA-256 | `957e08f3eb40052ac4283958b4d101b81ed0df824df39d4295779e216e11bc2e` |
| trial / max steps | 1 / 20 |
| 并发 | 1 |
| 变体 | baseline、harness、memory |
| 记忆抽取 | llm |
| 技能库 | 无（尚未生成真实训练技能） |

计划同时记录 24 个源码/脚本文件哈希、官方 commit 和模型文件哈希。源码、数据、模型配置或技能快照变化后旧计划会拒绝执行，必须新建。

## 复现命令

```bash
OPENCODE_API_KEY="<environment only>" \
python scripts/run_experiments.py plan \
  --dataset /path/to/prefix2/personalization/tasks.json \
  --upstream /path/to/VitaBench-2.0 \
  --model-config examples/opencode-deepseek-free.yaml \
  --model deepseek-v4.1-flash-free \
  --phase train --user-limit 1 \
  --variants baseline harness memory \
  --max-steps 20 --num-trials 1 --memory-extraction llm \
  --output artifacts/free-train-prefix2-final3

OPENCODE_API_KEY="<environment only>" \
python scripts/run_experiments.py check artifacts/free-train-prefix2-final3/plan.json
```

## 边界

- 这不是结果报告，只是可校验的实验配置。
- 没有 skills/full，因为没有真实训练技能库；不能用手工演示技能补足。
- 没有 baseline 真实分数；换模型后必须先重跑 baseline，再与新计划比较。
- 正式五组消融和保留测试仍需在额度恢复后新建计划并执行。
