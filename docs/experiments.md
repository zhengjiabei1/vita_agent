# 真实实验入口

`scripts/run_experiments.py` 运行真实官方 Agent/User/Evaluator，不会把模拟演示转成 benchmark 分数。`plan` 只做本地检查和记录；`execute` 才调用模型。所有实验组合使用同一 runner、同一模型配置、用户集合、seed、trial 数、步骤预算和并发。

**2026-10-27 状态：** 免费模型 `deepseek-v4.1-flash-free` 在直接 API 上返回 HTTP 429 `FreeUsageLimitError`。经用户明确授权后，付费 OpenCode Go 端点 `https://opencode.ai/inference/go/openai/v1` 连通成功；它要求额外的 `x-opencode-session` 请求头。预检验证了工具调用、JSON、106,659 Token 长上下文和 usage。

随后在新模型 `deepseek-v4.1-flash` 上完成 1 个训练用户 × 2 子任务的 baseline/harness/memory 各 1 trial，三臂均 **0.0**；共享 ledger 用 **176/200 请求**，估算 **0.30838665 USD**。真实结果、Token 和限制见 [reports/2026-10-27-go-train-prefix2](../reports/2026-10-27-go-train-prefix2/)。剩余 24 请求不足以完成有效 skills/full 五组消融；需要新额度并新建计划。免费额度阻塞记录见 [reports/2026-10-27-provider-quota](../reports/2026-10-27-provider-quota/)。

默认比较 full-context baseline、full-context + harness、structured memory、full-context + frozen skills、structured memory + harness + frozen skills。结构化记忆的当前轮提取属于记忆模块的处理；baseline 保留官方 full-context 记忆。技能单独收益只用 `skills - baseline`，不能用 `full - baseline` 代替。

## 数据与划分

官方 `personalization/tasks.json` 按真实 user_id 分组，固定 seed=42 划分 train/dev/test。同一用户若有多个 task，所有 task 都进入同一分区。manifest 保存任务文件 SHA-256、用户和任务 ID、各任务子任务数；不导出 instruction、rubric 或隐藏偏好到 Agent 额外上下文。

- `--phase train`：默认前三个训练用户，可用 `--user-limit 1` 限制，用于收集真实失败轨迹。先用 `--variants baseline`。
- `--phase smoke`：一个开发用户，调试实际链路。
- `--phase small`：最多三个开发用户，开发期消融。
- `--phase full`：全部保留测试用户。实现与参数冻结后才运行；建议 `--num-trials 4`。

在完整 56 用户数据集上，此规则得到 train=33、dev=11、test=12。正式结果是这 12 个保留用户的实验，不应描述为官方全部 56 用户排行榜成绩。

## 创建计划

```bash
.venv/bin/python scripts/run_experiments.py plan \
  --dataset /workspace/scratch/vitabench2-reference/data/vita/domains/personalization/tasks.json \
  --upstream /workspace/scratch/vitabench2-reference \
  --model-config examples/deepseek-flash.yaml --model deepseek-flash \
  --phase smoke --variants baseline harness memory --max-steps 20 \
  --output artifacts/smoke-v1
```

模型 YAML 必须声明该模型，设置明确的 `max_tokens`、`base_url`、安全环境变量形式的 `api_key`。付费模型必须且只能声明 `cost_1m_token_cny`（人民币）或 `cost_1m_token_dollar`（美元）的 `prompt_price` / `completion_price`；两种币种不能混用。免费模型必须声明 `billing: free` 和 `free_quota.unit`（`request` 或 `credit`），**不能**用 0 单价冒充免费，也不能同时声明 token 单价。计划只记录凭据变量名称与是否存在，不记录其值。缺少凭据允许生成计划，但禁止执行。`--memory-extraction llm|rules` 固定记忆抽取方式，默认 llm；非结构化记忆实验不使用该提取器。

加入 `skills` 或 `full` 时必须提供 `--skill-bank frozen.json`。加载器检查快照哈希，并要求全部来源任务都属于**本次数据划分的训练集**；演示中的手工失败技能无法通过此检查。训练期收集轨迹、开发期选择效用阈值后冻结；测试中只读加载。

计划还保存本项目源码/脚本校验和、官方固定 commit、模型文件哈希。实现、数据、配置或技能快照发生改变后旧计划拒绝执行，应建立新版本计划。输出目录必须为空；已存在的结果不会被覆盖，也不会触发上游自动 resume。

## 执行与费用

```bash
.venv/bin/python scripts/run_experiments.py check artifacts/smoke-v1/plan.json
.venv/bin/python scripts/run_experiments.py execute artifacts/smoke-v1/plan.json \
  --budget-cny 20 --budget-ledger artifacts/real-evaluation/shared-budget.sqlite3
```

执行前显式设置总预算：人民币用 `--budget-cny`，美元用 `--budget-usd`，必须与单价币种一致。训练/开发/测试的所有计划应复用同一个 `--budget-ledger`，保证 20 元是累计预算而非每个计划各 20 元。所有子进程在官方每次生成请求前按配置单价与输入/最大输出上界预留费用，记录实际 token 用量并结算；余额不足时停止发起新请求。它覆盖 Agent/User/Evaluator 使用官方客户端发出的请求，SDK 自动重试关闭。供应商隐藏费用、未报告用量或错误价格不受本地程序控制，供应商账户限额才是最终账单约束。

DeepSeek 配置使用官方峰时、未命中缓存的单价作为保守上界；缓存和低峰折扣可能使真实账单更低。结果标注 `cost_currency=CNY`、`cost_basis=peak_uncached_upper_bound`，不能把 token 估算称为已核对的供应商账单。原始官方配置展示和 checkpoint 会复制 LLM 参数；本 runner 会在日志与结果元数据中脱敏 API key/header，不修改实际推理配置。

### 免费请求/积分模式

免费模型使用同一 runner，但预算单位改为请求数或供应商积分：

```bash
# 计划与付费模式相同，模型 YAML 中使用 billing: free
OPENCODE_API_KEY="<environment only>" \
.venv/bin/python scripts/run_experiments.py plan \
  --dataset /path/to/personalization/tasks.json \
  --upstream /path/to/VitaBench-2.0 \
  --model-config examples/opencode-deepseek-free.yaml \
  --model deepseek-v4.1-flash-free \
  --phase train --user-limit 1 --variants baseline harness memory \
  --max-steps 20 --num-trials 1 --output artifacts/free-train-prefix2-v2

# 3 次请求是本地授权上限；不是供应商每日额度
OPENCODE_API_KEY="<environment only>" \
.venv/bin/python scripts/run_experiments.py execute artifacts/free-train-prefix2-v2/plan.json \
  --max-requests 3 --budget-ledger artifacts/free-shared.sqlite3
```

- `requests` 模式每次模型调用预留 1 个请求单位；`credits` 模式按 `free_quota.credit_per_request` 预留积分。
- Token 用量仍逐次记录，但没有 token 价格，也不会产生伪造的现金费用。
- 401/402/429 或明确的 `free usage limit`/`quota`/`rate limit` 会将共享 ledger 标记为 `halted_reason=provider_quota_exhausted`；之后同一 ledger 拒绝新请求，runner 停止，不自动重试。
- 训练、开发、测试、记忆抽取、模拟用户、评测器和离线反思必须复用同一 `--budget-ledger`，否则只是每个计划各自限额，不是总限额。
- 供应商日额度、模型可用性和实际扣减规则仍是最终约束；本地请求计数只防止超过你显式授权的上限。
- 先用 `scripts/verify_provider.py` 做工具调用、JSON、长上下文和 usage 预检；它同样走 ledger，遇到 429 立即停止并记录。预检通过只说明接口兼容，不是 benchmark 分数。

### 付费 OpenCode Go

如果免费额度不可用且用户明确授权，可用 `examples/opencode-go-deepseek.yaml`。该端点需要 `OPENCODE_SESSION_ID` 作为 `x-opencode-session` 请求头；API key 和 session 都只从环境读取。付费模式必须给现金上限，可同时给请求上限：

```bash
python scripts/verify_provider.py \
  --model-config examples/opencode-go-deepseek.yaml --model deepseek-v4.1-flash \
  --budget-usd 0.25 --ledger artifacts/go-preflight.sqlite3 --long-context-tokens 120000

python scripts/run_experiments.py execute artifacts/go-train-prefix2-v1/plan.json \
  --budget-usd 1.0 --max-requests 200 --budget-ledger artifacts/go-shared-budget.sqlite3
```

请求上限和现金上限同时生效；任一耗尽都会停止后续请求。实际 token 价格是配置估计，不是供应商发票。

### 离线反思与 v2 技能快照

```bash
OPENCODE_API_KEY="<environment only>" \
.venv/bin/python scripts/reflect_skills.py \
  --results artifacts/train-v2/baseline.json \
  --plan artifacts/train-v2/plan.json \
  --model-config examples/opencode-deepseek-free.yaml \
  --model deepseek-v4.1-flash-free \
  --output artifacts/skills.train.v2.json \
  --max-requests 12 --budget-ledger artifacts/free-shared.sqlite3
```

脚本只接受计划声明的 train 结果；每次反思请求计入共享 quota；候选必须引用原始 trace 文本并通过 schema 与原文引用校验。没有 grounded 候选时写诊断报告但不生成快照；请求失败或额度耗尽时不冻结任何部分结果。v2 快照已接入 `FrozenSkillBank.load()`，因此 `plan` 的 `--skill-bank`、`verify_plan` 和 worker 都能读取。技能收益仍需独立的 dev 筛选和 test 只读评估。

所有实验组合都会应用相同、固定源码校验的上游 taxi ID/渲染修复；计划和结果记录修复版本。这避免只让某组绕开官方工具缺陷。

每个组合单独输出结果 JSON 和日志。一个 subprocess 失败或结果不完整就停止，不自动重试，不丢弃失败样本。完成全部组合后生成 `comparisons.json`；主分数、update 子集与成本范围见 [evaluation.md](evaluation.md)。Agent cost 与总 API 账单不同，后者还包括模拟用户和 evaluator，应结合预算 ledger 报告。

没有模型服务和凭据时，`plan/check` 的阻塞信息就是当前状态；不能用单元测试通过数、本地模拟成功率或估算价格替代真实实验结果。

## 从真实训练失败提炼技能

```bash
.venv/bin/python -m vita_agent.traces --results artifacts/train-v1/baseline.json \
  --plan artifacts/train-v1/plan.json --output artifacts/train-failures.jsonl
.venv/bin/python -m vita_agent distill artifacts/train-failures.jsonl \
  --heldout test_task_ids.json --output artifacts/skills-real.v1.json
```

导出器只接受训练计划声明的结果，从明确 `role=tool,error=true` 且匹配已知错误类型的消息提取检查规则；未知错误、未评分运行和只有低 rubric 分数的运行都会统计并跳过。它不根据分数猜测失败原因，不把用户原话、参数或原始错误正文复制进技能。如果没有可提炼错误，就如实报告零条，不能用演示技能补足真实实验。
