# 免费模型额度阻塞记录

日期：2026-10-27（UTC 2026-10-26 19:16 请求）。模型：`deepseek-v4.1-flash-free`，OpenCode Zen 兼容端点 `opencode.ai`。

## 结果

一次最小工具调用预检就被供应商拒绝：

```text
HTTP 429
FreeUsageLimitError: Free usage limit reached.
Free models work in OpenCode; subscribe to OpenCode Go to keep going.
```

因此没有执行工具调用、JSON、长上下文或基准评测。付费模型 `deepseek-v4.1-flash` 另外返回 HTTP 402 `Insufficient account funds`；未继续尝试，也没有产生费用。

## 计数

[preflight.json](preflight.json) 由 `scripts/verify_provider.py` 生成（SHA-256 `98db14061c28be4495371c442be1b372a808b282506d273d1ba9f5d602339575`），使用独立请求 ledger：

- 限额 3 次请求，实际发起 1 次，返回 429。
- ledger：`held_requests=1`、`remaining_requests=2`、`counts={"uncertain":1}`、`halted=true`、`halted_reason=provider_quota_exhausted`。
- 没有成功响应，所以输入/输出 Token 均为 0；未记录任何提示或响应文本。
- 该 1 次请求按“已发起但未确认用量”保留为不确定，不计为成功，也不伪造 Token。

## 结论

- 免费额度在本次会话中已耗尽，必须停止免费真实模型调用，保存进度。
- 这不是代码、数据或评测失败；计费/额度控制在请求前正常拦截并 halt 共享 ledger。
- 用户随后明确授权付费 Go 模型；Go 端点 `https://opencode.ai/inference/go/openai/v1`（需要 `x-opencode-session`）连通成功，已完成 baseline/harness/memory 三臂训练 prefix2，三臂均 0.0。见 [reports/2026-10-27-go-train-prefix2](../2026-10-27-go-train-prefix2/)。
- 仍未完成 skills/full 和保留测试集；不能把旧 DeepSeek 分数或简历猜测值当作新结果。
- 免费模型后续恢复额度时应先重跑同模型 baseline，再按新计划执行；本仓库已提供请求数/积分模式、预检脚本和可复现的 prefix2 训练计划。
- 没有自动升级；付费 Go 使用是用户明确授权的，并受 200 请求和 1 USD 本地上限约束。

## 复现

```bash
export OPENCODE_API_KEY="<provided through environment, never commit>"
python scripts/verify_provider.py \
  --model-config examples/opencode-deepseek-free.yaml \
  --model deepseek-v4.1-flash-free \
  --max-requests 3 --ledger artifacts/provider-quota.sqlite3
```

不要在聊天、Git、日志或结果文件中写入密钥。供应商侧额度仍是最终约束。
