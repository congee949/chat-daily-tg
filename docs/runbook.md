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

### 订阅卡 ledger（Podcast 👍）

B站 / YouTube 订阅卡在 **r4s** 推送成功后 write-after-send 写入  
`/root/chat-daily/state/media_sent_ledger.jsonl`（`chat_id`+`message_id` → canonical URL）。  
**Mac** 侧 Podcast4bot 只读 `~/chat-daily/state/media_sent_ledger.jsonl`。  
拉取脚本：`scripts/sync_media_ledger.sh`（rsync，scp 回退；远端不存在则 skip exit 0）。  
launchd label：`com.chat-daily-tg.ledger-sync`（`StartInterval` 60s，`run_ledger_sync_guarded.sh`）。

### launchd label

| label | 时间 | wrapper |
|---|---|---|
| `com.chat-daily-tg.agent` | 7:05 触发，`--wait-for-wake` 单次探测 Watch 睡眠；无数据则立刻发总结 | `run_daily_guarded.sh` |
| `com.chat-daily-tg.channels` | 6,10,12,14,16,18,20,22 | `run_channels_guarded.sh` |
| `com.chat-daily-tg.growth` | 9:30 / 15:30 / 21:30 | `run_growth_guarded.sh` |
| `com.chat-daily-tg.growth-weekly` | 周六 9:45 | `run_growth_weekly_guarded.sh` |
| `com.chat-daily-tg.ledger-sync` | 每 60s（`StartInterval`） | `run_ledger_sync_guarded.sh` |

**永远经 guard wrapper 跑，不要让 plist 直调 python。** wrapper 负责 venv 预检（`.venv` 被 uv prune 时会静默 `exit 127`）、导出 http 代理、清 `ALL_PROXY`、开 `CHAT_DAILY_TG_ALERTS=1` 让告警能发出去、以及失败时 osascript + TG 双通道告警。2026-07-03 就发现过 channels 的 plist 是旧版、绕过了 wrapper。ledger-sync 例外：无 venv、不发 TG 告警（短 rsync，失败多半是瞬时 SSH）。

`install-launchd.sh` 装上表这 5 个，**不装** B站 / YouTube 的 label（已迁 r4s，脚本内有注释说明）。跑 installer 不会把它带回来，但也不要手动加。

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

当前 summary / verifier 走 VibeKey，vision / judge 走 CLIProxyAPI（`127.0.0.1:8317`）。切模型前先确认对应端点和模型列表可用。**这是 Mac 的配置；r4s 上 CLIProxyAPI 不存在，端点另有改写，见下方「部署到 r4s」。**

### 部署到 r4s

`deploy.sh` 现已带 `require_clean_tree` 守卫、detached-HEAD 检查和 `uv sync`（2026-06-29 修复），可以正常使用。

r4s 是 musl，两个坑：无 venv 模块（用 `pip3 --user`）；pypi 直连超时（走清华镜像）。

**r4s 的 `~/chat-daily/config.yaml` 与 Mac 不同源、手工维护**：`deploy` 只 `git archive` 代码，`config.yaml` 与 `.env` 独立留在 r4s 的 `~/chat-daily/`。因 CLIProxyAPI（`127.0.0.1:8317`）在 r4s 上**不存在**，`models` 的两个端点都做了部署改写——**任何指向 `127.0.0.1:8317` 的别名在 r4s 上都不可达，必须改写成公网端点（经 bwg tinyproxy 出口）**：

| 用途 | r4s 端点 | model / key |
|---|---|---|
| `models.vision`（有封面卡的一句话导读，**主路径**） | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash` / `GOOGLE_API_KEY` |
| `models.summary`（无封面卡才触发的**文本兜底档**） | `https://api.vibekey.cn/v1` | `gpt-5.6-luna` / `VIBEKEY_API_KEY` |

`models.summary` 在 2026-07-18 前是 `*gemini` 锚点，指向 Mac 本机 `127.0.0.1:8317`——r4s 上连接被拒 → 异常被 summarizer 的 try/except 吞掉 → 那张卡直接**没有 📝 摘要行**（等于没有兜底，非报错，符合「投递优先于完美」故长期没暴露）。当天修复：改直连 VibeKey `gpt-5.6-luna`，并在 r4s `.env` 补 `VIBEKEY_API_KEY`（此前**缺失**，缺了会 `KeyError`、同样降级为无摘要行）。所以 **r4s `.env` 必须含 `VIBEKEY_API_KEY`**。验证：`python3 run_daily.py --bilibili-only` 能干净跑通（exit 0），或用带 `models.summary` 的 `LLMClient` 直调一次确认端点+key+代理三者可达。

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
./scripts/install-launchd.sh     # 装 agent + channels + growth + growth-weekly
```

**注意**：`install-launchd.sh` 会 unload/reload 全部 label，且**没有 `schedule.py
apply` 那样的 in-flight 保护**——正在跑的任务会被打断。只改触发时间用上面的
`schedule.py apply`（有保护）；只想重装一个 label 则单独渲染那一个 plist，别跑整个脚本。
