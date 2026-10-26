# 2026-10-26 真实训练诊断快照

这份提交保存已完成实验的可核查证据，供换平台后继续诊断。**它不是正式测试结果，也没有证明任何性能提升。**

- 模型：`deepseek-flash`；seed=42；同一个训练用户的前 2 个子任务；baseline 与 harness 各 1 个 trial，共 4 次子任务执行。
- 两臂平均分均为 **0.0**，update 子集均为 **0.0**。只有 1 个用户，不报告用户级置信区间。
- 官方 `agent_cost` 聚合缺失，成本增幅保留 `null`；不能把已知费用项的和当作完整成本。
- 已完成两臂共 64 个模型请求，输入 1,355,720 Token、输出 12,249 Token，总计 1,367,969 Token。预算表按峰时未缓存单价估计 **2.809432 CNY**，不是核验过的供应商账单。
- 包括此前中断试跑的历史记录共有 157 个已结算请求，按上述口径估计 **8.658110 CNY**；另 1 个响应未读回的请求保留 **0.529772 CNY** 不确定预留。不能把预留当成确认扣款，也不能视为免费。
- memory、skills-only、full 三组尚未完成真实评测。原简历数字均未获实验验证。

## 文件与使用边界

`baseline.json` / `harness.json` 保留官方结果结构、选中训练任务、模型/工具轨迹、评分与 Token 用量，删除凭据和本机根路径。它们包含公开基准的训练样例内容及模型生成输出，仅供诊断这个训练试跑。

`comparisons.json` 是当时生成的配对报告。可以在安装本项目后离线重算：

```bash
python -m vita_agent compare \
  reports/2026-10-26-train-prefix2/baseline.json \
  reports/2026-10-26-train-prefix2/harness.json \
  --output artifacts/archived-prefix2-comparison.json
```

`plan.provenance.json` / `execution.provenance.json` 仅保存历史来源。**不要执行这些历史计划。** 原路径已替换为占位符，源码也可能更新；换模型、改配置、换数据或修改代码后都必须重新建计划，并在新模型上重跑 baseline。

`summary.json` 包含上述结果与费用汇总；`provenance.json` 记录每份原始文件与公开快照的 SHA-256、去敏变换和字段计数。费用汇总按 ledger 时间分段，原始 SQLite 留在原环境的忽略目录中，未公开；它不是账单凭证。

`dataset-audit.json` 是训练集解析/窄规则抽取覆盖诊断，**不是偏好召回率或模型分数**。`user-splits.json` 固定原始用户划分；`prefix2-derivation.json` 记录从完整数据取每用户前 2 子任务的变换与校验和。完整及派生数据集、运行日志、原始数据库、中断的试跑结果均未随本快照发布。

`export_evidence.py` 是导出脚本：原环境若仍保留 `artifacts/real-evaluation/`，从仓库根目录运行它可重新导出 JSON；脚本在任何写入前校验全部 8 份来源文件及预算数据库的历史 SHA-256；有后续调用、数据库迁移或来源变更时拒绝导出，避免污染这份固定快照。评分从原始比较报告读取，队列信息从历史计划推导。脚本使用 Python 3.11+，只读来源数据并在内存中查询校验过的预算数据库字节，不联网、不调用模型、不修改数据库。新平台只有仓库而无忽略目录时，应直接使用已提交快照，并按官方方式重新下载数据。不要从快照中恢复或寻找 API 密钥。

## 来源与许可

- 上游仓库：[meituan-longcat/VitaBench-2.0](https://github.com/meituan-longcat/VitaBench-2.0)，固定 commit `f60169e89f30499cb7883f3dad76bd03facc908d`。
- 官方数据：[Hugging Face 数据集](https://huggingface.co/datasets/meituan-longcat/VitaBench-2.0)。完整 `tasks.json` SHA-256 为 `3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e`（56 用户、771 子任务）。
- 上游仓库声明 MIT License；原许可文本随 [UPSTREAM-LICENSE](UPSTREAM-LICENSE) 保留。数据集使用条件仍应以官方数据集说明为准；此快照不重新许可第三方材料。

## 原始文件校验和

下表是去敏前的来源文件 SHA-256，公开文件的 SHA-256 另见 `provenance.json`。原始文件保持在忽略目录中，未覆盖。

| 公开文件 | 原始来源 SHA-256 |
| --- | --- |
| `baseline.json` | `0b5d472e786f857a5e4fbfa29779ee53691d02c9c05e0268ed7718ef141e29cc` |
| `harness.json` | `507211c46d203942706b2adbcf8ee6cbcb10fe27732fc833fe6df04d200e3da2` |
| `comparisons.json` | `fa2eabaa0b483e24410f6b974dfb00c9281bc59a5e1293c5e46a6c852fdd73c0` |
| `plan.provenance.json` | `b3d35d269ac238d2f5a7fd79d536f6ff5551d4d6e7c5b2ccaef1ccc01534683d` |
| `execution.provenance.json` | `78abcf65810e3902e6acac21fbb6d08e8ecbe5dab6b50ee57ae9c975f5235ead` |
| `dataset-audit.json` | `b85a1d72ca815225a1c2520def6dc9c52aaeba15aa16d19f98ab134e6b55fd65` |
| `user-splits.json` | `e09d1d2e71249b543f611118870c753d26fcf64a6b54427c4e45167fb5c995aa` |
| `prefix2-derivation.json` | `9eeb0590c69394f052b775f3a601b24f277aa51cc06a6b256c1e23c353115634` |
