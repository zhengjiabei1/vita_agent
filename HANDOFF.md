# 跨平台交接：继续完成真实 VitaBench 2.0 实验

更新日期：2026-10-26。仓库：<https://github.com/zhengjiabei1/vita_agent>。

## 目标与当前结论

实现 **Training-Free Personalized Service Agent on VitaBench 2.0**：执行前约束检查、带证据的结构化偏好记忆、从训练失败轨迹离线提炼并冻结的技能库。用户已说明简历中的 `0.440 → 0.570`、`+8.6%`、`0.38 → 0.48`、`4.6pp` 是猜测值；它们不是结果，也不应作为调参必须达到的目标。

当前是已有代码、单元测试和小规模真实训练试验的研究原型，**完整实验尚未完成，也没有证实性能提升**。截至本次交接，245 项测试通过；本地模拟演示成功；`ruff --select E4,E7,E9,F` 检查通过。未指定规则的全面 Ruff 检查仍有风格/类型建议，不能称为所有静态规则通过。

用户要求换平台/模型继续。此前 DeepSeek 总预算上限是 20 元，随后明确转向免费模型，付费调用已暂停。**不要恢复旧付费调用，不要把此前预算视为新平台的付费授权。** 密钥只通过环境设置提供，不放进聊天、Git、日志或结果文件。旧 `VITA_API_KEY` 绑定 DeepSeek 域名，不能直接拿到新供应商使用。

## 已完成的实现

| 文件 | 已实现内容 |
| --- | --- |
| `harness.py`, `guards.py` | draft/check/regenerate、有界重试、可信事实约束、SQLite 幂等日志、执行不确定时停止重试 |
| `official_facts.py`, `vitabench.py` | 官方 Agent/Memory 接口，30 个服务写工具的预检查适配、当前请求约束、工具观察事实；所有实验组共用包装器 |
| `memory.py`, `interactions.py`, `semantic_memory.py` | 用户证据、slot 覆盖链、硬约束注入、分层词项检索、可选 LLM 抽取、原始用户证据回退检索；不是 embedding RAG |
| `skills.py`, `traces.py` | 基于真实工具错误的训练轨迹导出、规则技能、效用淘汰、不可变 v1 快照、测试集隔离 |
| `skill_reflection.py` | 低分训练轨迹的 LLM 技能假设、原文引用校验、v2 冻结/加载函数；只做过模拟生成器测试 |
| `experiments.py`, `budget.py`, `evaluation.py` | 不可覆盖实验计划、源码/数据哈希、逐请求费用预留、未知用量保留额度、配对评测、update 子集、用户级 bootstrap、未知成本保持 null |
| `upstream_fixes.py` | 固定版本/文件哈希校验的 taxi 工具运行时修复，所有实验组一致使用；见 `docs/upstream_fixes.md` |

## 已有真实证据

先读 [已发布的训练试验证据](reports/2026-10-26-train-prefix2/README.md)，再读 [验证记录](docs/verification.md)。原环境的 `artifacts/`、`.venv/`、SQLite 预算数据库、官方完整数据集和模型凭据不随 Git 克隆迁移；去敏试验副本及来源哈希保存在 `reports/`。

- 官方仓库：<https://github.com/meituan-longcat/VitaBench-2.0>，固定 commit `f60169e89f30499cb7883f3dad76bd03facc908d`。
- 官方数据：<https://huggingface.co/datasets/meituan-longcat/VitaBench-2.0> 的 `tasks.json`；131234835 字节，SHA-256 `3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e`。
- 共 56 用户、771 子任务；seed=42 按用户划分 train=33/464 子任务、dev=11/149、test=12/158。保持原划分，不能因结果差而换用户。
- 训练历史解析审计：10004 条 interaction、0 解析错误、27762 条唯一用户话语；窄规则只提取到 1 条。这个数是抽取覆盖量，不是偏好召回率或任务成功率。
- 已完成试验用每个用户前 2 子任务的派生数据（共 56 用户/112 子任务），保留每个子任务全部历史；派生 SHA-256 `c76985f042b0be3f751e7a97261e3fe70f6e96402bc6de7f358e025433c523fe`。
- 真正完成的是 **1 个训练用户 × 2 子任务 × baseline/harness 两组 × 1 trial**，模型为 `deepseek-flash`（当时对应 DeepSeek-V4.1-Flash-1017）。两组平均分均 **0.0**，update 子集也均 **0.0**。这是训练诊断，不是独立测试集成绩。
- 上述完成试验共 64 次已结算模型请求，输入 1355720、输出 12249 Token。此前完整用户试跑中途停止，不能加入分数均值。具体费用分类以发布的汇总为准。
- 旧费用按峰时未命中缓存价格作上界估计，**不是供应商实际账单**。官方 `agent_cost` 有缺失，两组成本和增幅为 `null`，不能填 0 或声称 +8.6%。
- 没有完成 memory/skills/full 的真实对照、dev/test 正式评测、重复 trial；没有经真实训练/开发验证的冻结技能库。

## 下一步必须处理的断点

1. **免费配额控制。** `experiments.model_preflight()` 目前要求输入/输出单价严格大于 0，`budget.py` 以 CNY/USD 费用预留为主。免费 API 不能简单填 0，也不能伪造价格绕过校验。实现单独的免费模式：按请求数/供应商积分预留、记录 Token、单并发、明确额度上限、429/耗尽时停止、跨阶段共用额度账本；日额度恢复按供应商规则处理。重试、模拟用户、裁判、记忆抽取和离线反思都必须计入配额。
2. **确认新模型兼容性。** 明确服务、model ID、版本、上下文长度、工具调用、JSON、用量字段、限流和免费额度；先用少量请求做连通/工具调用/长上下文验证。所有实验组固定 Agent/User/Evaluator 配置并报告它们。换模型后重跑同模型 baseline，不能与旧 DeepSeek 分数直接比较模块收益。
3. **训练零分诊断。** 检查发布的训练轨迹、终止原因、任务评分和工具表现，分清模型行为、预检查阻塞、评测/适配问题。不能只增加样本量或改评分规则来制造提升。
4. **记忆覆盖验证。** 核验 LLM 抽取是否保留原文证据、更新覆盖与正确用户/领域；现有语义提取和回退检索尚无真实效果数据。不得从隐藏偏好/rubric 抽取在线记忆。
5. **反思技能集成。** `skill_reflection.load_reflected_bank()` 可加载 v2，但 `FrozenSkillBank.load()`、runner/CLI 尚未分派到它。完成兼容、预算接入和端到端测试后，只从训练失败生成技能假设；开发集评估效用/淘汰，再冻结供测试。当前工具错误导出器在已完成轨迹上得到 0 条，不能用手工演示技能冒充真实技能。
6. **正式实验。** 在训练/开发阶段确定实现和参数，再声明 baseline、harness、memory、skills、full 五组及 trial 数；只在冻结后运行保留测试用户。报告真实均值、update 子集、按用户置信区间、失败率、请求/Token/现金费用/积分消耗。小样本或不显著结果照实报告。

`budget.py` 已支持可选 `cached_prompt_price`，但历史 DeepSeek YAML 未启用；旧账本不能追溯改写成实际费用。原始试跑计划早于最近的缓存/反思代码更改，跨平台路径也不同，**只作历史证据，不能直接 execute**。每次改源码、数据、模型配置或技能库，都应重新创建计划。

## 新环境恢复

优先使用 Python 3.12（本次实际验证版本）。在新平台克隆本仓库，按照 `README.md` 安装核心开发依赖；完整官方集成可用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-vitabench.lock
python -m pip install -e '.[dev,vitabench]'
python -m pip check
python -m pytest -q
python -m ruff check --isolated --select E4,E7,E9,F src tests scripts
python -m vita_agent demo
```

锁定文件记录当前 Python 3.12 环境的第三方版本，未承诺在所有操作系统和 Python 版本可安装；若安装失败，记录实际错误并解决依赖，不静默更新评测依赖。新环境完整集成检查应确认官方依赖确已安装，不能把 `importorskip` 后的较少测试通过数误称为 245 项全通过。

另行克隆官方仓库并 checkout 固定 commit，保持其工作树不变。按官方 README 下载数据至该仓库的 `data/vita/domains/personalization/tasks.json`，验证上方 SHA-256。不要把 131 MB 数据集、密钥或本机绝对路径写进新提交。原环境参考路径是 `/workspace/scratch/vitabench2-reference`，新平台可选别的路径并通过 `--upstream`/`--dataset` 显式传入。

如果需要重建 prefix2 训练诊断数据：对完整任务列表中每个 task 仅执行 `task['subtasks'] = task['subtasks'][:2]`，其他字段不变；以 `json.dumps(tasks, ensure_ascii=False)` 的 UTF-8 编码写入（不加缩进或结尾换行），已验证得到上方派生哈希。文件仍应名为 `personalization/tasks.json`。新供应商初始验证应在训练集进行，先跑单用户/小规模样本。

详细规划命令见 [docs/experiments.md](docs/experiments.md)。`plan` 不调用模型；`execute` 才调用。现有文档中的 DeepSeek 执行示例是旧付费模式说明，不是恢复付费的授权。完成免费模式前，不应照抄该执行命令。

## 免费 Qwen 候选（仅资料核实，未接入实测）

2026-10-26 核实：魔搭官方 API-Inference 支持 OpenAI 兼容地址 `https://api-inference.modelscope.cn/v1/`，文档示例为 `Qwen/Qwen3.5-35B-A3B`。每日登录 200 魔粒，绑定阿里云账号后再获 50；API 约 0.5/1/2 魔粒每请求，按模型档位与当前规则扣减。需要绑定实名阿里云账号、验证邮箱。一次 benchmark 子任务包含多次模型请求；不能将每日请求额度当成任务数。模型可用性、额度和接口兼容性必须在新会话重新核验。

- <https://www.modelscope.cn/docs/model-service/API-Inference/intro>
- <https://www.modelscope.cn/docs/model-service/API-Inference/limits>
- <https://www.modelscope.cn/docs/magicube/intro>

Qwen Code 官方文档已注明 OAuth 免费套餐 2026-04-15 停用，不依赖旧“每日 2000 次”的教程。更换为其他免费平台也可以，但必须报告真实提供方和固定模型，不通过随机模型路由混跑实验。

## 可直接给下一位编程助手的提示词

```text
请接手 https://github.com/zhengjiabei1/vita_agent 的 main 分支，继续真实完成 Training-Free Personalized Service Agent on VitaBench 2.0。

先读 HANDOFF.md、README.md、docs/verification.md、docs/experiments.md，以及 reports/2026-10-26-train-prefix2/。检查代码和证据，不要从零重写。

简历中的 0.440→0.570、8.6%、0.38→0.48、4.6pp 都是猜测，禁止作为真实结果。当前245项测试通过；唯一完成的真实试验是1个训练用户、2子任务、baseline/harness各1trial，两组均0分，未完成正式测试和完整消融。

我要换平台/模型继续，优先免费API；旧DeepSeek付费调用已停止。先完成本地工作，再通过环境密钥设置获取实际需要的凭据，不能让我在聊天里贴Key，也不能未经授权产生费用。

按HANDOFF中的顺序执行：恢复固定官方版本与数据哈希并运行测试；实现真正的免费配额计数/停机模式（现有预算器不接受0单价，禁止伪造价格）；验证新模型工具调用、长上下文和用量字段；用训练轨迹诊断零分；验证结构化记忆；接通skill_reflection的v2冻结库与runner、计量反思成本；从训练失败提炼技能，在dev上选效用和淘汰策略，冻结后运行同模型五组消融及重复trial。固定按用户的train/dev/test划分，禁止测试泄漏。

跨模型需要重跑baseline；源码或模型配置变化后新建实验计划，不复用旧plan。免费额度耗尽就保存进度并停止，不自动付费。最后交付可复现代码、去敏原始结果、实验配置、真实分数与不确定性、请求/Token/现金及积分消耗，并按真实结果改写简历。若没有提升，明确报告，不编数字。
```
