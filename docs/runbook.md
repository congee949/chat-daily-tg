# 运维手册

本文讲**出事了怎么办**。系统怎么工作见 [ARCHITECTURE.md](ARCHITECTURE.md)，红线见仓库根 `CLAUDE.md`。

## 部署拓扑

| 机器 | 跑什么 | 出口 |
|---|---|---|
| **Mac**（本机） | 日报、频道转发、成长挖掘 —— launchd 4 个 label | http 代理 `127.0.0.1:1082`（Shadowrocket） |
| **r4s**（OpenWrt 主路由） | B站 digest —— cron 每小时 `:30` | TG/Gemini 经 bwg tinyproxy over tailscale；B站直连 |
| **bwg**（美国 VPS） | tinyproxy 出口 `100.87.113.14:8888` | —— |

代码在 Mac 的 `~/Projects/chat-daily-tg`，launchd **直接跑工作树源码**（不是安装副本），改完源码下次触发即生效，无需重装。r4s 上是 `/root/chat-daily-tg` 的独立副本。

数据与配置在 `~/chat-daily/`，独立于仓库，含密钥，不进版本控制。

### launchd label

| label | 时间 | wrapper |
|---|---|---|
| `com.chat-daily-tg.agent` | 7:05 + 9:00 / 13:00 catch-up | `run_daily_guarded.sh` |
| `com.chat-daily-tg.channels` | 6,10,12,14,16,18,20,22 | `run_channels_guarded.sh` |
| `com.chat-daily-tg.growth` | 9:30 / 15:30 / 21:30 | `run_growth_guarded.sh` |
| `com.chat-daily-tg.growth-weekly` | 周六 9:45 | `run_growth_weekly_guarded.sh` |

**永远经 guard wrapper 跑，不要让 plist 直调 python。** wrapper 负责 venv 预检（`.venv` 被 uv prune 时会静默 `exit 127`）、导出 http 代理、清 `ALL_PROXY`、开 `CHAT_DAILY_TG_ALERTS=1` 让告警能发出去、以及失败时 osascript + TG 双通道告警。2026-07-03 就发现过 channels 的 plist 是旧版、绕过了 wrapper。

`install-launchd.sh` 只装上表这 4 个，**不装** B站的 label（已迁 r4s，脚本内有注释说明）。跑 installer 不会把它带回来，但也不要手动加。

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
3. 9:00 / 13:00 两个 catch-up + `--skip-if-done`——兜住前两层都没盖住的情况。

**剩余盲区只有「电池 + 合盖」**：此时系统强制睡眠，任务跳过，靠 catch-up 在下次唤醒时补。重试网已扩为 `(HTTPStatusError, TransportError)` 涵盖 ProtocolError。

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

### 收到两条相同告警

预期行为。in-Python 优雅失败发一条，wrapper 捕获非零退出再发一条。视为告警系统的安全冗余，未消除。

## 常见操作

### 补跑某天

```bash
cd ~/Projects/chat-daily-tg
env -u ALL_PROXY -u all_proxy .venv/bin/python run_daily.py --date 2026-07-14
```

补跑会**重新生成不同的文本**（LLM 非确定），但 marker 保证每个阶段最多送达一次。`--no-push` 干跑不写 `.run-complete`，不会抑制后续 catch-up。

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

当前 summary / verifier 走 VibeKey，vision / judge 走 CLIProxyAPI（`127.0.0.1:8317`）。切模型前先确认对应端点和模型列表可用。

### 部署到 r4s

`deploy.sh` 现已带 `require_clean_tree` 守卫、detached-HEAD 检查和 `uv sync`（2026-06-29 修复），可以正常使用。

r4s 是 musl，两个坑：无 venv 模块（用 `pip3 --user`）；pypi 直连超时（走清华镜像）。

### 重装 launchd

```bash
./scripts/install-launchd.sh     # 装 agent + channels + growth + growth-weekly
```

**注意**：这会 unload/reload 全部 label。如果只想修一个，用 installer 的同一段渲染逻辑单独渲染那一个 plist，别跑整个脚本——它会打断正在跑的任务。
