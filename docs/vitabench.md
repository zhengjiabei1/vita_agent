# VitaBench 2.0 官方接口接入

已核对的官方仓库是 [meituan-longcat/VitaBench-2.0](https://github.com/meituan-longcat/VitaBench-2.0)，本实现对照提交 `f60169e89f30499cb7883f3dad76bd03facc908d`。官方 README 描述 56 个用户、771 个子任务，覆盖 delivery / instore / ota。数据集和模型服务需要另行配置；本仓库的本地演示不是官方评测结果。

## 安装与模型配置

先安装本项目，再在同一个 Python 环境安装固定版本的官方项目：

```bash
python -m pip install -e '.[dev]'
git clone https://github.com/meituan-longcat/VitaBench-2.0.git ../VitaBench-2.0
git -C ../VitaBench-2.0 checkout f60169e89f30499cb7883f3dad76bd03facc908d
python -m pip install -e ../VitaBench-2.0
```

当前云环境已将官方源码下载到 `/workspace/scratch/vitabench2-reference`，并在项目 `.venv` 中安装了官方依赖。已有 checkout 可直接使用，无需重复克隆。

也可以使用本项目的 `python -m pip install -e '.[dev,vitabench]'` 安装固定提交的官方包。`requirements-vitabench.lock` 记录本次 Python 3.12 / Linux 验证环境的 77 个第三方依赖版本；复现实验时可先安装该文件，再安装本项目和固定版本的官方项目。此锁文件不包含 API 凭据或本地绝对路径，也不替代官方数据版本记录。

按[官方数据集说明](https://huggingface.co/datasets/meituan-longcat/VitaBench-2.0)将文件下载至官方 checkout 的 `data/vita/domains/personalization/`。模型配置通过 `VITA_MODEL_CONFIG_PATH` 指向用户自己的 YAML。例如：

```yaml
default:
  base_url: https://api.openai.com/v1
  api_key: ${VITA_API_KEY}
  temperature: 0.0
models:
  - name: gpt-4.1
    max_tokens: 8192
    cost_1m_token_dollar:
      prompt_price: 2.0
      completion_price: 8.0
```

API 密钥从安全环境设置注入，勿写入代码、轨迹或配置文件。价格只是配置示例，运行前应核对所用服务的实际计费。需要允许数据集域名和所选模型 endpoint 的网络访问。官方代码在导入类型时也会加载模型配置；单元测试使用临时 `default: {}` 配置和模拟生成函数，不需要密钥、不发送 API 请求。

## 只接入结构化记忆

官方 `BaseMemory` 实现位于 `vita_agent.vitabench.StructuredPreferenceMemory`，直接使用官方扩展点：

```bash
vita run --domain personalization \
  --memory-class vita_agent.vitabench.StructuredPreferenceMemory \
  --agent-llm gpt-4.1 --user-llm gpt-4.1 --evaluator-llm gpt-4.1 \
  --num-tasks 1 --max-concurrency 1 --num-trials 1 \
  --save-to structured-memory/smoke.json
```

此命令仅替换记忆模块。官方 `vita.run` 直接构造 `PersonalizationAgent`，所以 `--memory-class` 本身不会启用执行前检查。

适配器接受官方两种历史格式：

- `Interaction` 对象或 `{type: "conversation", timestamp, content: [{role, content}, ...]}`，以及 `content.messages` / `content.dialogue`。
- `{date, dialogue: [{role, content}, ...], behavior: [...]}`。

只将 `role="user"` 的明确原话交给提取器。行为记录、助手回复、工具结果、未标明说话人的字符串、评测 rubric 和 groundtruth 不用于构造用户约束。格式或时间不合法时记录诊断并跳过。日期统一以 UTC 解析，原始时间顺序参与 supersede；同一对话的消息顺序使用微秒偏移消除时间并列。

每次官方任务 / trial 默认新建独立的内存 SQLite 数据库。即使用户 ID 相同，不同实例也不共享状态；缺失用户 ID 时生成实例 ID。需要落盘时显式传入 `path`，并为不同实验 / trial 使用独立路径。

偏好抽取支持 `VITA_MEMORY_EXTRACTION=rules|llm`。直接加载插件默认使用保守规则，实验 runner 的计划默认使用 `llm`，并将选择写入计划。规则覆盖明确的总预算、食物过敏、已知菜系偏好、明确正向的酒店品牌偏好（`hotel_brand`）和明确的电话叫醒需求（`hotel_amenities`）。包含“只认准/只住/必须”等排他/强制表达的品牌偏好记为 hard；普通喜欢记为 soft。语义抽取要求原文引用和 schema 合法，无法由确定性规则核实的硬约束降为候选，不能仅凭模型断言执行硬拦截。原始用户证据同时保存在独立历史库，按词项检索回退；全部被覆盖的旧证据不会重新注入。硬约束完整注入，软偏好做分层词法检索。没有使用官方 oracle 记忆，也不从订单行为自动推断硬约束。

训练集审计解析了 10,004 条交互记录，无解析错误；27,762 条去重用户原话中，加入品牌规则后规则抽取命中 18 条（17 条 `hotel_brand`、1 条 `total_price`），此前仅 1 条；电话叫醒规则单独统计。这是抽取覆盖统计，不是 gold 偏好召回率，也不是效果提升。带证据的语义抽取已实现，但还没有真实 memory 消融成绩。

## 启用 draft–check–regenerate 和冻结技能

真实实验统一使用 `scripts/run_experiments.py`，在自己的进程内替换官方 Agent 类，不修改官方源文件。此入口还提供预算拦截、凭据脱敏、固定版本校验和各组一致的上游补丁。先创建并检查计划：

```bash
python scripts/run_experiments.py plan \
  --dataset /path/to/VitaBench-2.0/data/vita/domains/personalization/tasks.json \
  --upstream /path/to/VitaBench-2.0 \
  --model-config /path/to/models.yaml --model MODEL_ID \
  --phase smoke --variants baseline harness memory \
  --output artifacts/smoke-v1
python scripts/run_experiments.py check artifacts/smoke-v1/plan.json
```

计划与检查不调用模型；显式执行和共享预算方式见 [experiments.md](experiments.md)。旧 `scripts/run_vitabench.py` 是低层接入示例，不提供完整实验入口的预算及脱敏保证，不能用它绕过这些保护。

`make_guarded_agent_class()` 返回真正继承官方 `PersonalizationAgent` 的类。每次生成的工具调用先检查，再返回给官方 orchestrator 执行。失败草案不执行，也不加入已执行的对话历史；校验反馈交给模型重生成，默认最多重生成两次。超限后返回未执行说明。所有生成尝试的 token 和 cost 累加至最终消息；任一尝试缺少 cost / usage 时保留 `None`，不将未知成本伪装为零。

官方适配器的 CGL 使用 `VITA_CGL_UNKNOWN_POLICY=warn`（默认）：**无法解析/没有可信事实时退回基线行为，只有明确解析出的违反才 BLOCK**。核心 `GuardedHarness` 仍默认 strict，供本地受控场景使用。这样避免“事实缺失”被误当成“违反约束”而无限重生成。酒店观察会解析 `hotel_name/tags`，支持 hard `hotel_brand` 和 `hotel_amenities`（电话叫醒）检查；创建订单会记录 `status`，当草案声称完成但仍有未支付订单时触发 `unpaid_order` 重生成。

可设置 `VITA_SKILL_BANK=/absolute/path/frozen-skills.json` 加载已冻结技能；实验 runner 用 `--skill-bank` 指定。只向 prompt 注入经过 schema 和 SHA-256 校验的 `lesson`，不注入来源用户 / task / trace 标识。评测进程只有只读 `FrozenSkillBank`，不蒸馏评测轨迹、不更新效用。

`skill_reflection.py` 支持基于训练轨迹引用生成候选技能与 v2 快照。**v2 已接入统一 `FrozenSkillBank.load()`**：`plan`、`verify_plan` 和 worker 都能读取 v2，同时保留 v1 格式。`scripts/reflect_skills.py` 负责真实反思调用、共享 quota/费用记账和失败时不冻结。尚未执行真实 LLM 反思、未生成真实 v2 技能库，也未测得技能收益；没有 grounded 候选时不会写快照。

消融组合：

| 实验 variant | 固定组合 |
| --- | --- |
| `baseline` | 官方 full-context memory，关闭执行检查，无技能 |
| `harness` | full-context memory，启用执行检查，无技能 |
| `memory` | structured memory，关闭执行检查，无技能 |
| `skills` | full-context memory，关闭执行检查，加载冻结技能 |
| `full` | structured memory，启用执行检查，加载冻结技能 |

所有组合使用同一 runner，并应用相同的 taxi ID/渲染运行时兼容补丁，详见 [upstream_fixes.md](upstream_fixes.md)。因此不能把本项目的 baseline 与未使用该补丁的公开成绩直接当成相同实验环境。

`VITA_ENABLE_GUARDS=0` 是显式实验开关，会关闭执行前检查。对比应固定模型、任务拆分、seed、trial 数、步骤预算与并发；每个组合重建独立记忆。训练 / 开发 / 测试须按用户分组，冻结技能前排除所有测试用户对应的任务。详见 [evaluation.md](evaluation.md)。

## 当前工具覆盖边界

官方观察值是自定义 `repr` 字符串，缺乏统一 JSON schema。`official_facts.py` 严格解析固定官方版本的已核对输出，覆盖 30 个写工具；不读取隐藏数据库或评测字段，不从草案中的价格生成事实。工具覆盖不等于所有业务规则或所有观察字段均完备。

| 写操作 | 事实来源与检查 |
| --- | --- |
| `create_delivery_order` | 必须先观察 `get_delivery_product_info`；按真实单价和数量算总额，验证 store/product 对应、库存、未来 dispatch_time、预算及可验证硬约束 |
| `create_hotel_order` | 必须先观察 `get_ota_hotel_info`；核对 hotel/room、真实价格、库存与非过去的房间日期 |
| 航班、火车、景点、到店商品、出租车创建订单 | 必须先观察对应商品或票务详情；核对实体、日期、数量、价格与可观察库存 |
| 各领域付款和取消 | 必须有本子任务已观察的订单；校验类型、所属用户和状态。取消不要求重新满足购买偏好 |
| 到店预订、预约、修改和取消 | 根据已观察店铺、预订或预约校验实体、时间、人数和状态；未公开容量保持未知 |
| 外卖备注修改、航班/火车日期修改 | 核对已观察订单、归属、状态与可验证的更新参数 |
| 官方标记为 READ / GENERIC / THINK 的工具 | 验证必要参数，按官方分类作为非提交操作 |
| 其他写操作 | 拒绝，必须新增明确的 schema 和可信事实映射后再开放 |

每次提交只允许一个写调用；同一子任务内相同写操作和参数不重复提交，包括先前结果不确定的情况。换子任务时清空产品、房间、订单观察和提交记录。创建订单后清空商品 / 房间缓存，付款后清空订单缓存。

不把商品标签当作完整过敏原清单。若已知硬约束需要的字段不在官方可观察输出中，检查会拒绝执行并要求进一步信息。跨日期酒店多晚聚合、未公开容量及复杂业务规则仍有边界。执行前事实检查不能保证现实世界系统中的原子库存锁定。核心本地 Harness 另有持久化幂等日志；官方适配器的提交记录只在当前任务进程内有效。

## 已验证与未验证

`tests/test_vitabench.py` 覆盖两种历史格式、用户角色过滤、跨实例隔离、supersede、官方 memory factory、真实 Agent 继承关系、重生成前拦截、成本累加、当前轮偏好更新、冻结技能注入和过去日期拒绝。真实官方类测试对 `generate` 使用模拟替身，工具绝不执行、LLM API 不调用。

已下载并核对官方完整数据集，固定 seed=42 按用户划分为 train 33 用户 / 464 子任务、dev 11 / 149、test 12 / 158。已完成真实 DeepSeek 的训练集 prefix2 试验：1 个训练用户、前 2 个子任务、1 次 trial，baseline 与 harness 均为 0.0；官方 `agent_cost` 聚合缺失，成本增长率为 `null`。一个较早的训练试跑已中止，不能作为完整评分结果。零分诊断见 [reports/2026-10-27-zero-score-diagnosis](../reports/2026-10-27-zero-score-diagnosis/)：baseline 选择了高 GI 外卖和错误酒店品牌；harness 多次阻塞且最终无外卖订单。

尚未完成开发集、保留测试集或五组真实消融，因此没有复现 `0.440 → 0.570`、`+8.6%` 成本、`0.38 → 0.48` 或 `+4.6pp`；这些数字不作为本实现的成绩。OpenCode 免费额度返回 429 后，经用户授权在 Go 端点 `deepseek-v4.1-flash` 上完成训练 prefix2 baseline/harness/memory 三臂，均 **0.0**；176/200 请求，估算 0.30838665 USD。详见 [reports/2026-10-27-go-train-prefix2](../reports/2026-10-27-go-train-prefix2/) 和 [reports/2026-10-27-provider-quota](../reports/2026-10-27-provider-quota/)。继续开发见 [HANDOFF.md](../HANDOFF.md)。
