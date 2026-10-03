# 2026-09-25 模型目录变更导致的日报与成长挖掘故障

## 现象

- 2026-09-25 09:30 的成长挖掘使用 `--model sol`，请求 `gpt-5.6-sol` 返回 `400 model_not_found`。
- 2026-09-25 09:24 的日报视觉阶段使用 `gpt-5.6-luna`。CLIProxyAPI 连续返回 400，视觉熔断提前结束，19 次请求中 12 次失败，剩余 35 张候选图未尝试。
- 同一轮日报摘要主模型也使用 `gpt-5.6-sol` 并返回 `model_not_found`，随后 qwenproxy 完成摘要与推送。归档中的 `2026/09/24/.run-complete`、`.digest-sent` 和 `summary.md` 证明该次重跑已交付。
- 09:30:55 的 `exit=120` 告警发生在 `.run-complete` 写入之后，属于守护层对已交付进程退出状态的误判。

## 证据

2026-09-25 运行时 `http://127.0.0.1:8317/v1/models` 返回 `gpt-6-sol`、`gpt-6-luna` 和 `gemini-3.7-flash-high`，不再包含 `gpt-5.6-sol` 或 `gpt-5.6-luna`。使用归档 JPEG 的真实视觉请求和文本请求验证了 `gpt-6-luna`、`gpt-6-sol` 均返回 HTTP 200。

## 修复

- `~/chat-daily/config.yaml`：`sol.model` 和 `models.summary` 锚点改为 `gpt-6-sol`；`models.vision.model` 改为 `gpt-6-luna`。
- 修改前配置保存在 `~/chat-daily/config.yaml.backup-20260925-model-catalog`。
- `scripts/run_daily_guarded.sh`：退出码为 120 且本次运行新写入 `.run-complete` 时归一为成功；其他非零退出仍告警。
- `docs/ARCHITECTURE.md`、`docs/runbook.md`：成长 wrapper 的模型别名和模型目录检查方法与当前入口一致。

## 验证

- 通过项目 `LLMClient` 实测 `sol → gpt-6-sol` 和 `sonnet → gpt-6-luna` 文本请求。
- 通过项目 `VisionClient` 使用真实 JPEG 实测 `gpt-6-luna` 结构化视觉结果。
- `run_daily.py --growth-only --model sol --no-push` 完成 2026-09-24 挖掘，`gpt-6-sol` 返回候选 4 条并生成 A/B 卡与 judge 结果；未向 Telegram 投递。
- `env -u ALL_PROXY -u all_proxy uv run --extra dev pytest -q tests/test_daily_guard_diagnostics.py tests/test_growth_wrappers.py tests/test_llm_client.py tests/test_vision.py tests/test_config.py`：95 passed，覆盖 exit=120 有/无新鲜 marker。

## 正式补跑

2026-09-25 09:56:54 执行 `run_daily.py --growth-only --model sol`，日志记录成长卡 `2026-09-24-1838101`（style A）发送到 growth 话题。SQLite 只读快照显示该段 `status=sent`、`sent_at=2026-09-25T09:56:54+08:00`，当天已送出 1 张。日报 `2026/09/24/.run-complete` 已存在，`--skip-if-done` 验证直接返回 0，未重复推送。
