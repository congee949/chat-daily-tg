# chat-daily-tg

把微信和 Telegram 内容整理、筛选并投递到 Telegram。代码在本仓库；运行数据、配置和
密钥在 `~/chat-daily/`，不进版本控制。

## 运行边界

- Mac 运行日报、频道转发、成长挖掘和 ledger-sync（launchd）。
- r4s 运行 B站、YouTube 订阅 digest（cron）；不要在 Mac 恢复这两个定时任务。
- r4s 的 `media_sent_ledger.jsonl` 是权威源；Mac 副本只供 Podcast4Bot 读取，会被同步覆盖。
- TG 话题路由的事实源是 `~/qwenproxy/.tg-notify-targets.json`；修改后用
  `scripts/sync_tg_targets.sh` 同步，远端副本只读。

## 不可破坏的约束

### 投递与状态

- 投递优先于增强功能。图片、卡片、富消息、持久化或派生视图失败时必须降级，不能挡住正文。
- `seen` 必须 write-after-send。只有明确的终态抑制可在不发送时推进 seen，并同时写入
  `~/chat-daily/state/dedup_journal.jsonl`。
- 相册的每个消息 ID 都要写 seen，不能只记首条。
- 保持现有阶段 marker 语义；`.run-complete` 仅在真实推送成功后写，`--no-push` 不算交付。
- LLM 输出不能直接信任。凡业务逻辑依赖其结构或枚举，都要有代码级解析、归一化和回退。
- vision 阈值已校准：`min_include_score=0.8`、`fallback_min_score=0.65`；没有明确需求不要改。

### 网络

- B站 API 与封面 CDN 必须直连，`httpx` 使用 `trust_env=False`；海外代理出口会触发风控。
- YouTube、Telegram、Gemini 等海外服务按运行环境使用 HTTP 代理，不要复用 B站 client。
- 所有正式入口继续清除继承的 `ALL_PROXY/all_proxy`；不要绕过 guard wrapper。
- 429、B站 `-352` 等限流只做有界退避或降频，不绕过、不无限重试。

### 配置与部署

- 密钥只放 `~/chat-daily/.env`，权限保持 600；仓库、plist、日志和示例不得出现真实密钥。
- Mac 与 r4s 的 `~/chat-daily/config.yaml` 独立维护；模型、endpoint 和 key 必须按目标机器验证。
- launchd 必须调用 `scripts/run_*_guarded.sh`，不能让 plist 直接调用 Python。
- `scripts/install-launchd.sh` 当前安装 5 个 Mac label：agent、channels、growth、
  growth-weekly、ledger-sync。执行会 reload，可能打断在途任务；只改时间优先用
  `python scripts/schedule.py apply` 的 in-flight 保护。
- r4s 是 BusyBox/OpenWrt + musl；cron 使用 `TZ=CST-8`，wrapper 内的锁和 due-gate
  负责防重入与随机间隔。不要假设存在 systemd 或命名时区支持。

## 工作方式

- 开始前先看 `git status`；当前工作树可能含用户未提交改动，不要覆盖或顺手整理无关文件。
- 先验证代码、有效配置、目标机器进程/日志，再参考历史文档；不要把模型列表、测试通过或
  本机结果当作生产投递成功。
- 常用验证：

```bash
env -u ALL_PROXY -u all_proxy uv run --extra dev pytest -q
python run_daily.py --no-push
./scripts/sync_tg_targets.sh --check
python scripts/schedule.py list
```

- 涉及投递链路时，除测试外还要检查对应日志、marker/seen/ledger，以及实际 Telegram 结果。
- 未经用户明确要求，不修改 `~/chat-daily/`、远端配置、cron/launchd 或执行生产部署。

## 深入文档

- 系统数据流与状态机：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 部署、日志与故障排查：[docs/runbook.md](docs/runbook.md)
- 安装、配置与 CLI：[README.md](README.md)
- Mac 定时事实源：[schedule.yaml](schedule.yaml)
- 历史设计与实现记录：`docs/spark/`、`docs/notes/`（仅供追溯，不代表当前状态）
