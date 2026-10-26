# 验证记录与真实实验边界

2026-10-27 在新环境重新运行完整测试：`python -m pytest -q`，**268 passed，无 skip**（已安装固定官方 VitaBench 包）。测试中的模型生成使用替身，不产生推理费用，也不构成 benchmark 成绩。

本次交接还重新通过以下本地检查：

- `python -m ruff check --isolated --select E4,E7,E9,F src tests scripts`：指定范围通过；未指定规则的全面检查仍有风格/类型建议，未全部通过。
- `python -m pip check`：无依赖冲突。
- 固定官方仓库 commit `f60169e89f30499cb7883f3dad76bd03facc908d`；完整 `tasks.json` 重新下载并核对 SHA-256 `3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e`（56 用户、771 子任务）；按 seed=42 得到 train 33/464、dev 11/149、test 12/158。
- 重新派生每用户前 2 子任务数据，SHA-256 `c76985f042b0be3f751e7a97261e3fe70f6e96402bc6de7f358e025433c523fe`，与历史快照一致。
- `python -m vita_agent demo`：本地外卖、酒店流程完成，输出 `benchmark_results=null`。
- `python scripts/run_experiments.py plan ...` 与 `check`：免费模型 prefix2 训练计划可创建、可校验，不调用模型。
- `python scripts/verify_provider.py ...`：OpenCode 免费模型一次最小工具调用返回 HTTP 429 `FreeUsageLimitError`；ledger 立即 halt，后续请求被拒绝。用户明确授权付费 Go 后，端点 `https://opencode.ai/inference/go/openai/v1`（需 `x-opencode-session`）预检通过：工具调用 1 次、JSON 解析成功、106,659 Token 长上下文命中目标串，usage 字段完整。免费阻塞记录见 [reports/2026-10-27-provider-quota](../reports/2026-10-27-provider-quota/)。
- 新 Go 模型真实训练 prefix2：1 个训练用户 × 2 子任务 × baseline/harness/memory 各 1 trial，三臂均 **0.0**，update 子集均 0.0；无用户级置信区间。共享 ledger 用 176/200 请求，输入 1,828,931、输出 56,745 Token，估算 0.30838665 USD。详见 [reports/2026-10-27-go-train-prefix2](../reports/2026-10-27-go-train-prefix2/)。
- 训练零分诊断脚本和规则技能导出：archived baseline/harness 两臂均 `exported=0`，原因是没有任何已识别工具错误；新 Go 三臂也无工具错误。没有用演示技能补足。见 [reports/2026-10-27-zero-score-diagnosis](../reports/2026-10-27-zero-score-diagnosis/)。

此前还验证过无第三方依赖演示和官方 runner 的 `--help` 加载。上述检查验证安装与本地运行能力，不代表完整数据集评测通过。演示生成物位于被 Git 忽略的 `artifacts/`，可用 `python -m vita_agent demo` 重新生成。

测试涵盖偏好证据与时间覆盖、用户/领域隔离、硬约束注入、否定/引用语句过滤、未知事实拒绝、预算/过敏/日期检查、跨领域草稿与错误实体类型、重生成、所有草稿计费、幂等重放、执行结果不确定时禁止重试、技能来源/冻结/淘汰、完整评测样本校验与缺失成本处理。后续新增的覆盖包括 30 个官方写工具的映射、真实本地 taxi 工具生命周期、语义记忆抽取校验、原文回退、训练轨迹反思及缓存感知预算结算。

## 已完成的真实数据工作

- 官方代码固定于 `f60169e89f30499cb7883f3dad76bd03facc908d`；已下载并核对官方数据：56 个用户、771 个子任务。
- 数据文件 SHA-256：`3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e`。
- 按用户、seed=42 划分 train 33 用户 / 464 子任务、dev 11 / 149、test 12 / 158。
- 训练集解析审计：10,004 条交互记录，无解析错误；27,762 条去重用户原话中，加入明确酒店品牌正向偏好规则后，规则抽取命中 18 条（17 条 `hotel_brand`、1 条 `total_price`），此前仅 1 条。这是规则覆盖统计，不是偏好召回率或效果提升。
- 已完成真实 DeepSeek 训练集 prefix2 试验：1 个训练用户、前 2 个子任务、1 次 trial，baseline 和 harness 两组平均分均为 **0.0**；update 子集也均为 0.0。零分诊断显示 baseline 选择高 GI 外卖和非喜来登酒店，harness 多次阻塞且最终无外卖订单，未发现评分器缺陷。
- 上述结果来自训练集的短前缀试验，不是保留测试集成绩。两组官方 `agent_cost` 聚合缺失，成本增长率保持 `null`；不能据此声称成本无增加。
- 较早一次较长训练试跑已中止，没有完整评分结果，不能与完成试验混报。

已保存的 token 成本按配置的 DeepSeek 峰时未命中缓存单价估算，**不是供应商实际扣款账单**；缓存与低峰折扣可能降低实际费用。实验结果摘要与来源哈希见 [reports/2026-10-26-train-prefix2](../reports/2026-10-26-train-prefix2/)。

## 尚未完成

没有完成开发集或保留测试集评估，也没有 skills/full 的真实消融结果。`skill_reflection.py` 的 v2 反思快照已接入统一加载器和实验计划校验，`scripts/reflect_skills.py` 也已接入共享 quota 记账；但尚未执行真实 LLM 反思、尚未生成真实 v2 技能库，也没有测得技能收益。新 Go 模型上已完成 baseline/harness/memory 的训练 prefix2 三臂，均 0.0；剩余 24/200 请求不足以完成有效 dev 技能筛选和五组消融，因此停止，等待新额度。

付费模型调用已经暂停。简历中的 `0.440 → 0.570`、`+8.6%`、`0.38 → 0.48` 和 `4.6pp` 是用户说明的猜测值，没有实验支持，不能作为本项目成绩。后续任务与迁移说明见 [HANDOFF.md](../HANDOFF.md)。
