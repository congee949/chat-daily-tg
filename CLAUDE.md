# CLAUDE.md — chat-daily-tg

每天把微信 / Telegram 群消息整理成日报推送到 Telegram，另有频道转发、成长内容挖掘、B站订阅 digest 三条管线。
代码在本仓库（Mac），数据与配置在 `~/chat-daily/`（独立于仓库，含密钥，**不进版本控制**）。
B站 digest 已迁至 r4s cron，不在 Mac 上跑。

---

## 硬规则

违反以下任一条都是真实事故，不是风格问题。

### 网络与代理

- **B站请求一律直连，绝不走代理。** `httpx` 必须 `trust_env=False`，包括 hdslb 封面 CDN 下载。海外出口 IP 会触发 -352 风控。
- **`ALL_PROXY` 必须在入口清除。** `run_daily.py` 的 `__main__` 第一行调用 `env.scrub_socks_proxy_env()`（只 pop `ALL_PROXY`/`all_proxy`，保留 http 代理变量）。venv 的 httpx 无 socksio extra，带 socks5 的 `ALL_PROXY` 会让 `httpx.Client()` **构造即抛 ImportError**，在 `NO_PROXY` 求值之前。
  - **不要装 socksio 来"解决"它**——装了流量会真走 socks5，偏离已验证的 http 代理配置。scrub 是精确恢复已知可用状态。
- **不绕过限流，只降频。** 429 按 `retry_after` 退避且重试次数有界（无界重试会挂死整个 launchd run）；B站 -352 是 IP 级判决，首个 UP 命中即中止本轮。

### LLM 输出信任边界

- **LLM 产出的结构必须有 code-level 兜底，prompt 约束只作辅助。** LLM 已反复证明会违反格式约定。现有兜底先例：`_extract_fences`（fence 解析状态机）、`coerce_enum`、`_normalize_score`（0-10 制 / 百分制 / bool 归一化）、`resolve_citations`（`[IMGn]` 去重与未知 id 剥除）。新增任何依赖 LLM 输出格式的逻辑，都要配一层解析兜底。
- **vision 的 0.8 入选线不要随手改。** 它是 2026-07-02 用户亲自从 0.65 调上来的，用于挡糊图。零图天靠 `fallback_min_score=0.65` 保底提升一张，被模型否决（model-veto）和 empty-filter 的图不参与保底。

### 投递与幂等

- **投递优先于完美（rather deliver than drop）。** 每个增强阶段（卡片、图片、富消息、派生视图、持久化）都要 try/except 包裹并有回落路径，任何一步失败都不得阻塞正文日报送达。
- **seen 一律 write-after-send。** 发送成功才写入 `raw_seen.SeenStore`，失败不写、下轮重试。**相册的每个成员 id 都要写**，不能只写 head——只记 head 会让增量高水位 `max_msg_id` 卡在相册首条，下次重抓尾部媒体重复推送。
- **幂等用 day-level 阶段 marker，不做内容 hash。** 现有：`.run-complete` / `.persisted` / `.card-sent` / `.digest-sent`。语义是"当天某阶段最多送达一次"，与内容无关（catch-up 重跑会生成不同文本）。
  - `.run-complete` **仅在 push 成功时才写**——`--no-push` 调试跑不算交付，不得抑制补跑。

### 配置与密钥

- **密钥只进 `~/chat-daily/.env`（权限 600）。** `config.yaml`、launchd plist、仓库里只写 env 变量名，永不写真实值。日志经 `logging_setup.redact()` / `_RedactingFormatter` 脱敏（它同时清洗 message 和 exception traceback——httpx 报错会把 bot token 嵌在 URL 里）。
- **TG 话题路由表的唯一事实源是 Mac 上的 `~/qwenproxy/.tg-notify-targets.json`。** 改完必须跑 `./scripts/sync_tg_targets.sh` 同步到 r4s / bwg；`--check` 只看 diff 不推送。远端副本一律视为只读派生物。

### 部署

- **`install-launchd.sh` 故意不装 bilibili label**（B站 digest 已迁 r4s cron，脚本内有注释说明）。它装且只装 4 个：agent / channels / growth / growth-weekly。**不要把 bilibili 加回 Mac**，会造成双跑重复推送。
- **r4s cron 必须自带 flock。** cron 没有 launchd 的同 label 防重入，慢轮次会与下一轮重叠。`run_bilibili_r4s.sh` 在脚本内部持锁（`/tmp/chat-daily-bilibili.lock`），不在 crontab 行上。
- **r4s 是 musl 环境，命名时区会静默回退 UTC。** cron 内必须用 POSIX 形式 `TZ=CST-8`，写 `Asia/Shanghai` 会产生 8 小时偏差。

---

## 速查表

### 环境变量（`~/chat-daily/.env`）

| 变量 | 用途 |
|---|---|
| `VIBEKEY_API_KEY` | VibeKey（日报 summary / verifier） |
| `CLIPROXY_API_KEY` | 本机 CLIProxyAPI（vision / grok judge 共用） |
| `DEEPSEEK_API_KEY` | `llm` 别名，growth 挖掘与 B 卡 |
| `GOOGLE_API_KEY` | Gemini embedding 证据检索 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | Telegram bot 推送 |
| `CF_KV_API_TOKEN` | Cloudflare KV 图片中转（富消息内嵌图） |
| `VISION_API_KEY` | 旧 qwenproxy vision 路径，当前未用（config 一行可切回） |

### 模型别名（`~/chat-daily/config.yaml`）

| 别名 | 实际模型 | 用途 |
|---|---|---|
| `models.summary` | gpt-5.6-sol @ `api.vibekey.cn` | 日报摘要与核验 |
| `models.vision` | gemini-3.5-flash-low @ `127.0.0.1:8317` | 图片理解 |
| `llm` | deepseek-v4-pro @ `api.deepseek.com` | growth 挖掘 / B 卡（质量敏感，wrapper 固定传 `--model llm`） |
| `grok` | grok-4.5 @ `127.0.0.1:8317` | growth A/B judge（**异源**：作者与评审必须分厂） |
| `models.embedding` | gemini-embedding-2 | 高风险 claim 证据检索 |

日报正文与核验走 VibeKey。CLIProxyAPI（`127.0.0.1:8317`）继续服务 vision / judge；它挂了图片理解会降级，但不再直接阻断日报正文。`NO_PROXY` 必须放行 `127.0.0.1`。

### TG 话题路由表

事实源 `~/qwenproxy/.tg-notify-targets.json`，`chat_id = -1004424841223`。**下表是 2026-07-15 实测快照，以文件为准，不以本表为准**：

| key | thread | key | thread |
|---|---|---|---|
| `alert` | 10 | `bilibili` | 486 |
| `chat_daily` | 17 | `growth` | 497 |
| `twitter` | 19 | `cc98` | 1079 |
| `files` | 23 | `ai_cn` | 1145 |
| `macrumors` / `channels_news` | 41 | `market_recap` / `biz` | 1146 |

### launchd（Mac，2026-07-15 实测）

| label | 时间 | 职责 |
|---|---|---|
| `com.chat-daily-tg.agent` | 6:30 + 9:00 / 13:00 catch-up | 日报总结（`--skip-if-done` 防重复交付） |
| `com.chat-daily-tg.channels` | 6,10,12,14,16,18,20,22 | 频道增量转发（`--channels-only`） |
| `com.chat-daily-tg.growth` | 9:30 / 15:30 / 21:30 | 成长内容挖掘 |
| `com.chat-daily-tg.growth-weekly` | 周六 9:45 | 周报 + rubric 合并 |

B站 digest 在 r4s cron 每小时 :30，不在此表。

睡眠防护三层：`caffeinate -is`（wrapper 内，防 idle）+ `com.chat-daily-tg.disablesleep` **root LaunchDaemon**（插电时合盖也不睡；需 sudo，故不在 `install-launchd.sh` 里）+ 9:00/13:00 catch-up。**剩余盲区只有「电池 + 合盖」。**

launchd 同 label 不并发，睡过的触发点唤醒时合并，无需锁。

### vision 阈值

`min_prefilter_score=0.45` → `min_include_score=0.8` → `fallback_min_score=0.65`（零图天保底）。
进 vision 前有 `_is_valid_image_file` 门槛：≥10KB 且 ≥300×300，PIL 解析失败即判无效（挡微信缩略图与 `wxgf` 私有格式坏文件）。

### 常用命令

```bash
python run_daily.py                      # 跑昨天日报
python run_daily.py --date 2026-07-01    # 补跑指定日期
python run_daily.py --no-push            # 干跑，不推送（不写 .run-complete）
python run_daily.py --channels-only      # 只跑频道转发
pytest -v                                # 全量测试
./scripts/sync_tg_targets.sh --check     # 看 fleet 路由表 diff
./scripts/install-launchd.sh             # 装 agent + channels 两个 label
```

`deploy.sh` 现已带 `require_clean_tree` 守卫、detached-HEAD 检查和 `uv sync`（2026-06-29 修复），可以正常使用。

---

## 深入文档

| 主题 | 位置 |
|---|---|
| **系统怎么工作**：四条管线数据流、推送阶梯、幂等状态机、代理拓扑 | `docs/ARCHITECTURE.md` |
| **出事了怎么办**：部署拓扑、日志、故障排查、补跑与路由表同步 | `docs/runbook.md` |
| 功能清单 / 安装 / 配置样例 / 归档产物 | `README.md` |
| TG 统一路由表设计 | `docs/spark/2026-07-09-unified-tg-route-table-design.md` |
| LLM 输出信任边界校验设计 | `docs/spark/2026-07-09-llm-output-validation-design.md` |
| B站订阅 digest 设计（含 §18.4 回滚路径） | `docs/spark/2026-07-02-bilibili-subscriptions-design.md` |
| 历次实现决策、取舍与审查报告（**已冻结的历史归档**，不反映现状） | `docs/notes/`（见其 `README.md`） |
