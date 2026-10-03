# 运维手册

本文讲**出事了怎么办**。系统怎么工作见 [ARCHITECTURE.md](ARCHITECTURE.md)，红线见仓库根 `CLAUDE.md`。

## 部署拓扑

| 机器 | 跑什么 | 出口 |
|---|---|---|
| **Mac**（本机） | 日报、频道转发、成长挖掘、ledger-sync —— launchd 5 个 label | http 代理 `127.0.0.1:1082`（Shadowrocket） |
| **r4s**（OpenWrt 主路由） | B站 / YouTube digest —— cron | TG/Gemini 经 bwg tinyproxy over tailscale；B站直连 |
| **bwg**（美国 VPS） | tinyproxy 出口 `100.87.113.14:8888` | —— |

代码在 Mac 的 `~/Projects/chat-daily-tg`，launchd **直接跑工作树源码**（不是安装副本），改完源码下次触发即生效，无需重装。r4s 上是 `/root/chat-daily-tg` 的独立副本。

数据与配置在 `~/chat-daily/`，独立于仓库，含密钥，不进版本控制。

<a id="ledger-sync"></a>

### 账本同步

`com.chat-daily-tg.ledger-sync` 每 60 秒调用
[run_ledger_sync_guarded.sh](../scripts/run_ledger_sync_guarded.sh)，仅通过
[sync_sent_content_ledger.sh](../scripts/sync_sent_content_ledger.sh) 把 Mac
送达账本推送到 R4S，供 Hermes 读取。wrapper 返回推送脚本的退出码。
X caption 快照由频道 wrapper 持锁刷新，见
[去重运行指南](dedup-policy.md#x-caption-镜像)。

B站 / YouTube 订阅卡仍在 R4S 发送成功后写入权威 `media_sent_ledger.jsonl`。
Mac 副本保留为历史知识索引输入，不再每分钟自动拉取。需要刷新时显式运行
[sync_media_ledger.sh](../scripts/sync_media_ledger.sh)；先用 `--check` 检查两侧，
再执行拉取。该脚本保留 JSONL 校验、缩量保护和原子替换。

旧 Podcast Telegram Bot 已退役；账本和历史 Podcast 资料不随入口退役删除。
人工补发订阅卡的 write-after-send 记录仍应写入 R4S 权威账本。

### launchd label（Mac）

日历时间的事实源是仓库根 `schedule.yaml`（`python scripts/schedule.py list` 对比已装 plist）。当前：

| label | 时间 | wrapper |
|---|---|---|
| `com.chat-daily-tg.agent` | 7:05 触发，`--wait-for-wake` 单次探测 Watch 睡眠；无数据则立刻发总结 | `run_daily_guarded.sh` |
| `com.chat-daily-tg.channels` | **00:00, 02:00, 06:00, 09:00, 10:00, 12:00, 14:00, 16:00, 18:00, 20:00, 22:00**（00:00、02:00 覆盖夜间 X/频道晚到；09:00 缩短 06→10 早间空窗）；实际发送 = 触发 + wrapper 0–15min jitter | `run_channels_guarded.sh` |
| `com.chat-daily-tg.growth` | 9:30 / 15:30 / 21:30；**`--model sol`**（模型 ID 由 `~/chat-daily/config.yaml` 的 `sol.model` 指定） | `run_growth_guarded.sh` |
| `com.chat-daily-tg.growth-weekly` | 周六 9:45；同样 **`--model sol`**（模型 ID 由 `~/chat-daily/config.yaml` 的 `sol.model` 指定） | `run_growth_weekly_guarded.sh` |
| `com.chat-daily-tg.ledger-sync` | 每 60s（`StartInterval`） | `run_ledger_sync_guarded.sh` |

**永远经 guard wrapper 跑，不要让 plist 直调 python。** wrapper 负责 venv 预检（`.venv` 被 uv prune 时会静默 `exit 127`）、导出 http 代理、清 `ALL_PROXY`、开 `CHAT_DAILY_TG_ALERTS=1` 让告警能发出去、以及失败时 osascript + TG 双通道告警。2026-07-03 就发现过 channels 的 plist 是旧版、绕过了 wrapper。ledger-sync 例外：无 venv、不发 TG 告警（短 rsync，失败多半是瞬时 SSH）。

`install-launchd.sh` 装上表这 **5** 个，**不装** B站 / YouTube 的 label（已迁 r4s）。跑 installer 不会把 bilibili/youtube 带回来，也**禁止**手动在 Mac 恢复这两个 label（双跑会重复推卡）。

### r4s cron（B站 / YouTube only）

B站与 YouTube **只**在 r4s 跑。探测是 `*/5`，真正执行由 `scripts/due_gate.sh` 的随机间隔门控：

| 任务 | due_gate 间隔 | wrapper |
|---|---|---|
| bilibili | **20–30 min** | `run_bilibili_r4s.sh` |
| youtube | **10–15 min** | `run_youtube_r4s.sh` |

成功后才推进下次 due；失败保持可重试。这**不是**旧文档里的“每小时 :30”。cron 必须 `TZ=CST-8`，锁防重入。`media_sent_ledger.jsonl` 以 r4s 为权威，Mac 历史索引副本按需同步。

### LaunchDaemon（root 级，install-launchd.sh 装不了）

`com.chat-daily-tg.disablesleep` 装在 **`/Library/LaunchDaemons/`**（不是 `~/Library/LaunchAgents/`），以 root 每 60s 调一次 `scripts/power_aware_disablesleep.sh`。

它按电源动态开关 `pmset disablesleep`：**插电 = 1**（合盖不睡，8 个调度点全覆盖），**拔电 = 0**（恢复正常睡眠，带出门装包里不过热、不空耗电）。`pmset disablesleep` 是全局开关、不分电源档，所以只能这样动态切；脚本只在目标值与当前值不同时才写，避免每分钟无谓调用。

**它需要 sudo，所以不在 `install-launchd.sh` 里**——`launchd/com.chat-daily-tg.disablesleep.plist` 是模板（含 `REPLACE_WITH_PROJECT_DIR` / `REPLACE_WITH_DATA_DIR` 占位符），手工渲染后放进 `/Library/LaunchDaemons/` 并 `sudo launchctl load`。

确认它在工作：

```bash
ls /Library/LaunchDaemons/com.chat-daily-tg.disablesleep.plist
pmset -g | grep SleepDisabled     # 插电时应为 1，拔电时为 0
log show --predicate 'process == "logger"' --last 1h | grep cd-disablesleep
```

## 日志

| 文件 | 内容 |
|---|---|
| `~/chat-daily/logs/YYYY-MM-DD.log` | 日报管线主日志 |
| `~/chat-daily/logs/channels-YYYY-MM-DD.log` | 频道转发 |
| `~/chat-daily/logs/growth-YYYY-MM-DD.log` | 成长挖掘 |
| `~/chat-daily/logs/guard-*-YYYY-MM-DD.log` | 各 wrapper 的 guard 层日志（venv 预检、退出码） |
| `~/chat-daily/logs/stdout.log` / `stderr.log` | launchd 兜底 |

日志经 `_RedactingFormatter` 脱敏，同时清洗 message 和 exception traceback（httpx 报错会把 bot token 嵌在 URL 里）。

## 日常检查

**今天的日报发出去了吗？**

```bash
ls ~/chat-daily/archive/2026/07/15/.run-complete    # 存在 = 整轮成功且已推送
```

marker 语义见 [ARCHITECTURE.md 的幂等小节](ARCHITECTURE.md#幂等day-level-阶段-marker)。`.digest-sent` 有但 `.run-complete` 没有，说明正文送达后崩在了收尾。

**为什么今天日报没图？**

```bash
grep "vision analyses included" ~/chat-daily/logs/2026-07-15.log
cat ~/chat-daily/archive/2026/07/15/vision-audit.jsonl | head
```

`vision-audit.jsonl` 记录全量候选（含落选与失败），`breakdown` 里能看到 `below_bar` / `model_veto` / `filtered_empty` / `api_failed` 各多少。**0.8 门槛下约一半天数是零图天，这是常态不是故障**——只有 `attempted>0 且 api_failed>0 且 included==0` 才会告警。

## 故障排查

### 三条管线同时挂，报 `No module named 'socksio'`

**根因**：Shadowrocket 经 `launchctl setenv` 把 `ALL_PROXY=socks5://…` 写进了 launchd 用户环境。venv 的 httpx 无 socksio extra，`httpx.Client()` **构造即抛 ImportError**，在 `NO_PROXY` 求值之前。

**处置**：正常情况 `scrub_socks_proxy_env()` 已在 `run_daily.py` `__main__` 第一行自愈。若仍复现，检查是不是绕过了入口（比如直接 import 模块跑脚本）。

**不要装 socksio 来"解决"**——装了流量会真走 socks5，偏离已验证的 http 代理配置。

手动跑测试时同理：`env -u ALL_PROXY -u all_proxy uv run --extra dev pytest -q`。

### 任务静默不跑，退出码 127

`.venv` 被 `uv prune` 或依赖变更清掉了。guard wrapper 有 venv 预检会告警；如果没收到告警，先确认这个 label 的 plist 是不是绕过了 wrapper：

```bash
plutil -p ~/Library/LaunchAgents/com.chat-daily-tg.channels.plist | grep -A5 ProgramArguments
```

应该指向 `cdrun-bash` + guard wrapper，不是直接 `python`。修法是重装那**一个** label，不要跑整个 installer（会 unload 正在跑的其他任务）。

### 日报已推送但守护告警 `exit=120`

先检查当天归档的 `.run-complete`。日报 wrapper 已固定子进程的 stdout/stderr 到
`~/chat-daily/logs/stderr.log`；如果仍收到 120，但本次运行刚写入 `.run-complete`，wrapper
会把它归一为成功，并在 `guard-YYYY-MM-DD.log` 记录 `normalized child exit=120`。没有新鲜
`.run-complete` 时，120 仍按失败处理，先查看日报日志和 `stderr.log`，再补跑未交付日期。

### 日报没发，日志显示 `RemoteProtocolError`

**根因**：MacBook 合盖睡眠。请求发出后进程入睡，DarkWake 醒来时代理 TCP 已被对端断开。

**三层防护，各管一段**：

1. `caffeinate -is`（wrapper 内）——防 idle/AC 睡眠，**防不了合盖**。
2. `com.chat-daily-tg.disablesleep` LaunchDaemon（见下）——**插电时**合盖也不睡。
3. wake-gate 循环本身 + launchd 触发合并——7:05 被睡过时 launchd 在唤醒后补发触发（`--skip-if-done` 挡已交付日）；等待中入睡则进程冻结，唤醒后循环继续、当场投递。原 9:00/13:00 catch-up 触发点已由此取代（2026-07-17）。

**剩余盲区只有「电池 + 合盖」**：此时系统强制睡眠，任务跳过或冻结，靠下次唤醒时的触发补发/循环恢复来补。重试网已扩为 `(HTTPStatusError, TransportError)` 涵盖 ProtocolError。

**替代唤醒方案均已否决**（别再提）：`pmset repeat wakeorpoweron` 需 root 写系统级持久状态且 dark wake 撑不住 20+ 分钟的 run；`caffeinate -u` 会点亮屏幕，7:05 无人值守不可接受；Power Nap 的 dark-wake 窗口由系统支配、无法按 job 控制——这次故障恰恰就是在这种窗口里跑出来的。

被采纳的是 `pmset disablesleep`，但它是全局开关、不分电源档，直接开会让电池合盖也禁睡（装包里过热）。所以做成了上面那个按电源动态切换的 LaunchDaemon。

### B站全部 UP 抓取失败 / -352

-352 是 **IP 级风控判决**。首个 UP 命中即中止本轮，不对已风控 IP 连打 22 次。

**处置：降频，不绕过。** 先确认没有走代理——B站请求必须 `trust_env=False` 直连（含 hdslb 封面 CDN），海外出口即风控。r4s 上检查 `run_bilibili_r4s.sh` 的 `NO_PROXY` 设置。

### 私有频道 dump 超时（600s）

单个文件下载卡住会拖垮整个频道。已有防护：`tg_media_dump.py` 给每个 `download_media` 包了 `asyncio.wait_for(timeout=45)`，慢/失败文件跳过当文字处理。

若整频道仍超时，多半是增量高水位失效导致重抓当天全部媒体——检查 `SeenStore.max_msg_id` 是否正常。

### r4s 上时间差 8 小时

**根因**：r4s 是 musl 环境，命名时区（`Asia/Shanghai`）会**静默回退 UTC**。

**处置**：cron 里必须用 POSIX 形式 `TZ=CST-8`。

### 日报中途消失，无 traceback / 无 marker / 当天不重试

**根因（2026-07-18 事故）**：有人在日报正跑到 vision 阶段（push 之前）时执行了
`python scripts/schedule.py apply`。旧版 `apply` 对每个 label 无条件 `launchctl
unload` → `load`，而 `unload` 会给该 label **正在运行的进程发 SIGTERM 并回收**——
bash wrapper 一起被杀，到不了上报行，于是极其隐蔽：无 traceback、无 crash report、
无 guard 心跳、无阶段 marker，agent 单一 07:05 触发当天也不重试。定位靠 plist
mtime（≈ 中断时刻）比对进程死亡时间。

**已修（触发源）**：`apply` 重载前加了 in-flight 保护——`job_running(label)` 用
`launchctl list <label>` 探活跃 PID，正在跑（或状态拿不准）就**跳过该 label 的
unload/load** 并告警、退出码非 0；确认要强杀才加 `--force`。另有幂等：已装 plist
与将写入内容逐字节相同的 label 直接跳过。所以正常情况下 `apply` 不会再打断在飞行
的 run。

**排查现场**：若已发生（用了旧版、或 `--force` 强杀），先看
`~/chat-daily/logs/agent-stderr.log` 末尾是否戛然而止、`~/Library/LaunchAgents/
com.chat-daily-tg.agent.plist` 的 mtime 是否落在日报运行窗口内。run_daily.py 现有
SIGTERM handler（commit aa4c57a）会把这类中断转成告警——收到「被 SIGTERM 中断」
告警即此类。**补跑**：`python run_daily.py --date <当天>`（`--skip-if-done` 会挡已
交付日，中断未交付则正常补发）。

### 收到两条相同告警

预期行为。in-Python 优雅失败发一条，wrapper 捕获非零退出再发一条。视为告警系统的安全冗余，未消除。

## 常见操作

### AmbiguousDelivery / 漏推核对

当 TG 写超时（`ReadTimeout` / `WriteTimeout` / `RemoteProtocolError` 等）时，Bot API 可能已接受消息但本地无响应。目标态：

| 管线 | 超时策略 | 自动重试 POST？ | 状态推进 |
|---|---|---|---|
| channels（公开+私有） | `AmbiguousDeliveryError` + `_terminalize_ambiguous_delivery` | **否** | 成员 id 写 seen + `dedup_journal` `action=ambiguous` + 告警 |
| growth 日卡 / 周报 | 本地实现不盲重 POST，歧义状态需核验 | **否** | 日卡靠 claim/mark_sent；周报见下方 weekly marker |
| bilibili / youtube digest | **目标态**同语义（r4s 需同步部署） | **否** | seen / journal 分流；勿只在 Mac 改代码就当 r4s 已生效 |

告警标题常见「投递结果待确认」。先到目标话题人工核对有没有那条消息，再决定补发。

### 公开频道文本补发（`channels resend`）

`chat-daily channels resend -- "chat_id:msg_id"`（或 `run_daily.py --resend`）**只覆盖公开频道文本卡**：

- 绕过 SeenStore / 高水位 / L1–L2 去重，重建并再发**一条**卡片。
- 成功后写 seen。
- **不**覆盖私有频道媒体帖；媒体-only 私有消息 `build_card` 会返回 None。

步骤：

1. 从告警或 `~/chat-daily/state/dedup_journal.jsonl` 取 `chat_id` / `msg_id` / channel。
2. 在目标话题确认确实缺失（避免双发）。
3. `env -u ALL_PROXY -u all_proxy .venv/bin/python -m chat_daily_tg.cli channels resend -- "<chat_id>:<msg_id>"`
4. 看 `channels-*.log` / resend 日志确认 re-delivered。

### 私有媒体 Ambiguous / 漏推补发 SOP

`channels resend` **不能**安全重放私有媒体（需 telethon 再 dump + bot 再上传）。

1. **核对**：目标话题是否已有该帖/相册；对照 `dedup_journal` 的 `member_ids` 与频道原始 `msg_id`。
2. **已存在**：什么都不要做。seen 终态化是正确的防双发。
3. **确实缺失**（定向补，勿整窗重跑）：
   - 用配置中的该 `RawChannel`（无 username = 私有）缩小时间窗，临时把 `SeenStore` 中对应 keys 视为“待补”时须**逐条**处理——不要整文件回滚高水位。
   - 优先：对单帖/相册做一次受控 private dump + 发送（`scripts/tg_media_dump.py` / 现有 private 路径），caption 与原帖一致；相册每个成员 id 成功后 write-after-send 写 seen。
   - 若只能整频道 catch-up：确认 `min_id`/高水位不会把已送达帖再扫一遍；或先备份 `raw_seen` 再只放开缺失区间。
4. **禁止**：对 Ambiguous 结果盲目重跑 `channels` 全量；禁止在未核对前 `seen` 回滚。
5. **记录**：补发成功后在 journal 旁注或运维笔记留下 message_id / 时间，避免下次再当漏推。

### 成长周报幂等（`weekly-*.sent`）

当前行为由 [application.py](../src/chat_daily_tg/application.py) 的 `run_growth_weekly` 定义。调度见 [schedule.yaml](../schedule.yaml)，运行别名见 [周报 wrapper](../scripts/run_growth_weekly_guarded.sh)。

正式运行先检查 `growth/weekly-YYYY-Www.sent`。同周 marker 存在时，在消费反馈和调用模型前返回。确认发送后 marker 保存 `status=delivered`、目标、message IDs 与时间；发送结果不确定时保存 `status=ambiguous` 并告警，后续不自动重发。核验时读取 marker 内容并对照目标消息，不能仅按文件存在判断送达。

`--no-push` 不写周报 marker，但当前实现仍会读取和消费反馈、调用模型并生成待复审 rubric 候选；它不是只读预览。成长日卡的 `--no-push` 也可能先挖掘并写入片段，再跳过发送。隔离检查应使用测试数据目录与 mock 环境。

反馈合并失败时已取出的 inbox 内容会恢复，已轮换文件保存在 `growth/feedback-processed-*.jsonl`。反馈状态、rubric 版本与投递 marker 分别核验，迁移时同时保留。

### 补跑某天

```bash
cd ~/Projects/chat-daily-tg
env -u ALL_PROXY -u all_proxy .venv/bin/python run_daily.py --date 2026-07-14
```

补跑会**重新生成不同的文本**（LLM 非确定），但 marker 保证每个阶段最多送达一次。`--no-push` 干跑不写 `.run-complete`，不会抑制后续补跑。手动补跑不带 `--wait-for-wake`，立即执行不等信号。

### 改 TG 话题路由

唯一事实源是 Mac 上的 `~/qwenproxy/.tg-notify-targets.json`。改完必须同步：

```bash
./scripts/sync_tg_targets.sh --check     # 先看 r4s / bwg 的 diff
./scripts/sync_tg_targets.sh             # 推送
```

脚本会校验 JSON 合法性（绝不把坏表推向 fleet）、逐台显示 diff、推送后回读校验。远端副本一律视为只读派生物，不要直接在 r4s/bwg 上改。

新建话题用 Bot API `createForumTopic`（bot 有 manage_topics 权限），拿到 `thread_id` 回写这张表再同步。

### 切换模型

改 `~/chat-daily/config.yaml` 的 `models.summary` / `models.vision`，或顶层别名 `vibekey` / `llm` / `grok`。别名对照见 README 的「模型配置」。

当前 Mac 的 summary、verifier、vision 和 growth judge 都走 CLIProxyAPI（`127.0.0.1:8317`）；summary/growth 使用 `sol.model`，vision 使用 `models.vision.model`。切模型前先用带认证的 `/v1/models` 响应确认精确模型 ID 可用，再做真实文本/图片探针：

```bash
set -a; . ~/chat-daily/.env; set +a
curl -fsS -H "Authorization: Bearer $CLIPROXY_API_KEY" http://127.0.0.1:8317/v1/models
```

2026-09-25 的模型目录变更与当次修复记录见 [过程笔记](notes/incident-2026-09-25-model-catalog.md)。Mac 使用本地转发地址；r4s 使用 BWG 的 Tailscale 地址，见下方「部署到 r4s」。

### 部署到 r4s

`deploy.sh` 现已带 `require_clean_tree` 守卫、detached-HEAD 检查和 `uv sync`（2026-06-29 修复），可以正常使用。

r4s 是 musl，两个坑：无 venv 模块（用 `pip3 --user`）；pypi 直连超时（走清华镜像）。

#### 待部署 R4S 清单（Ambiguous / 调度语义）

Mac 工作树改动**不会**自动出现在 r4s。涉及 digests 的语义同步前，按项勾：

- [ ] 同步代码到 `/root/chat-daily-tg`（`deploy.sh` 或受控 archive）；**不要**指望只改 Mac。
- [ ] 确认 bilibili / youtube 推送路径具备与 channels 一致的 **AmbiguousDelivery** 语义：超时**不**自动重 POST；歧义写 journal/告警并抑制自动重放；成功路径仍 write-after-send 写 seen + `media_sent_ledger`。
- [ ] cron 仍是 `*/5` + `due_gate`（B站 20–30min / YT 10–15min），**不要**改回死板 `30 * * * *` 当唯一门控；`TZ=CST-8` + flock。
- [ ] r4s `config.yaml` / `.env` 独立校验：`CLIPROXY_API_KEY`、`YOUTUBE_API_KEY`、CPA 直连、外部服务代理与 B站直连（`trust_env=False`）保持正确。
- [ ] 受控真发：各跑一轮 bilibili / youtube，核对卡片、seen、ledger 行；确认 R4S 权威账本可解析；需要更新 Mac 历史索引时显式拉取。
- [ ] 确认 Mac **没有**加载 `com.chat-daily-tg.bilibili` / `youtube`（`launchctl list | grep chat-daily`）。

r4s 的 `~/chat-daily/config.yaml` 与 Mac 独立维护；代码部署保留目标机器的配置和 `.env`。
r4s 通过 BWG 的 Tailscale 地址调用 CPA，凭据使用目标机器 `.env` 中的
`CLIPROXY_API_KEY`。本机回环地址只表示 r4s 自身。模型配置与网络规则见
[YouTube 官方频道指南](youtube-selection.md)，切换实测见
[2026-10-01 验证报告](notes/2026-10-01-youtube-selection-verification.md)。
之前的公网模型路线保存在[历史记录](process/2026-10-01-r4s-model-route-history.md)。

**r4s `.env` 还必须含 `YOUTUBE_API_KEY`**（2026-07-21 起）：YouTube digest 的
`sources.youtube.api_key_env` 从 `GOOGLE_API_KEY` 拆出——`GOOGLE_API_KEY`
（项目 520642803598）被 console 限制在 Gemini API，调 youtube.v3 一律 403；
新建的 `YOUTUBE_API_KEY` 只放行 YouTube Data API v3（RSS 全灭时发现频道上传
列表，以及补时长/播放量、Shorts 过滤）。两个 key 各司其职，**不要合并也不要互换**：
新 key 调 Gemini 同样 403。
缺失时 enrichment 自动降级 watch-page 抓取（不报错、卡片缺时长），与
YouTube API key 缺失时沿用上述降级规则。验证：`curl --proxy http://100.87.113.14:8888
"https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id=GlYgs6v2YfU&key=$YOUTUBE_API_KEY"`
返回 200。

### 改 4 个 label 的触发时间

改仓库根 `schedule.yaml`（单一事实源）再 `apply`：

```bash
python scripts/schedule.py list      # 对比 yaml ↔ 已装 plist
python scripts/schedule.py apply -n  # 干跑，只打印将写入的时间
python scripts/schedule.py apply     # 写模板 + 重装 + reload
```

`apply` 有 in-flight 保护：某 label 的 job 正在跑（或 `launchctl list` 状态拿不准）
就**跳过它的重载**并告警、退出码非 0——`launchctl unload` 会 SIGTERM 掉在飞行中的
run（见故障排查「日报中途消失」）。此时**等该 label 无 run 时重跑 `apply`** 即可；
确认要强杀才加 `--force`。已装 plist 逐字节相同的 label 也会跳过（幂等，不重载）。

### 重装 launchd

```bash
./scripts/install-launchd.sh     # 装 agent + channels + growth + growth-weekly + ledger-sync
```

**注意**：`install-launchd.sh` 会 unload/reload 全部 label，且**没有 `schedule.py
apply` 那样的 in-flight 保护**——正在跑的任务会被打断。只改触发时间用上面的
`schedule.py apply`（有保护）；只想重装一个 label 则单独渲染那一个 plist，别跑整个脚本。
