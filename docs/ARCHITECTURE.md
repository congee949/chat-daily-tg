# 架构

本文讲**系统怎么工作**。规则和红线在仓库根 `CLAUDE.md`，运维操作在 [runbook.md](runbook.md)。

## 全景

四条管线，一个入口（`run_daily.py`），靠 flag 分流：

| 管线 | flag | 触发 | 产物 |
|---|---|---|---|
| 每日日报 | 默认 | launchd 7:05，`--wait-for-wake` 单次探测睡眠；无数据立刻发 | 一条 TG 富消息（含内嵌图） |
| 频道转发 | `--channels-only` | launchd 6–22 每 2 小时 | 每条频道消息一张卡片 |
| 成长挖掘 | `--growth-only` / `--growth-weekly` | launchd 9:30/15:30/21:30，周六 9:45 | 每天最多一张成长卡 |
| B站 digest | `--bilibili-only` | **r4s cron** 每小时 :30 | 每个新视频一张卡片 |

四条共用：`~/chat-daily/config.yaml` 配置、`chat-daily.db` 数据层、`~/qwenproxy/.tg-notify-targets.json` 路由表、`archive/YYYY/MM/DD/` 归档、`notifier` 告警。

它们**互不阻塞**：频道转发不依赖日报是否有内容（历史上耦合过，2026-06-06 解开），成长挖掘失败不影响日报，B站已整个搬到另一台机器。

## 每日日报管线

`run_daily._run()` 的实际顺序：

```
load_env(.env) → load_config
  ↓
cleanup_old_media(14 天)            ← try/except，失败只 warning
  ↓
校验有 summary 源                    ← 只配 raw_channels 会在此 fail fast
  ↓
prepare_archive_day → archive/YYYY/MM/DD/
  ↓
微信导出（per group 隔离）  ─┐
Telegram 导出（per chat 隔离）├→ groups_with_content + media_candidates
  └ vision 开启时 telethon 旁路下图 ─┘
  ↓
无内容 → return 1
  ↓
vision 阶段                          ← 整段 try/except + notify_failure
  analyze_media_candidates → vision.jsonl（仅入选）+ vision-audit.jsonl（全量）
  build_citation_block → citation_map（喂给 LLM 的可引用图片池）
  ↓
上下文拼装
  跨群聚类 + 长期机会库 + 短期热点 + 重复话题 + embedding 证据检索（可选）
  ↓
run_summary（初稿 → verifier 二次核验）
  ↓
落盘 concise.md / summary.md / verification.json / fact-risk-report.md
  ↓
post_process → 短于 100 字符则告警 return 1
  ↓
持久化机会（.persisted 门）+ 重生成派生视图  ← try/except，失败不阻塞推送
  ↓
推送（见下）
  ↓
.run-complete                        ← 仅 --no-push 未开时才写
```

### 关键设计

**导出是 per-source 隔离的。** 单个群导出失败只 `continue`，不影响其他群。只有**全部**源都没内容才中止。

**tg-cli 不存媒体，图片走旁路。** tg-cli 的 `messages.db` 架构上只有文本（`raw_json` 全表为空，实测 0/42713 行有值），图在 sync 阶段就丢了。所以 vision 开启时另起 `telegram_media.export_chat_media`，借 kabi-tg-cli 的 telethon 解释器和登录 session 直连下图。**经过验证的 tg-cli 文本链路完全不动**——这是"补充式旁路"而非改造。

**微信侧是"先打分后下载"，Telegram 侧是"先下载后筛选"。** 两边不对称是有意的：`wx attachments --json` 的 `local_id` 与导出文本里的占位符是同一 ID，所以能在拿到文件前先用纯文本关键词打分（`score_media_context`），只下达标的。代价是"图很好但配文很短"的情况会误伤，Telegram 侧靠全下载 + vision 二次筛能兜住，微信侧为控制体积放弃了这个兜底。

**vision 是三级漏斗**：`min_prefilter_score=0.45`（文本打分）→ `min_include_score=0.8`（模型打分）→ 零图天用 `fallback_min_score=0.65` 保底提升一张。进 vision 前还有 `_is_valid_image_file` 硬门槛（≥10KB 且 ≥300×300，PIL 解析失败即判无效），用来挡微信缩略图和 `wxgf` 私有格式坏文件。

0.8 这条线下**约一半的天数是零图天**，这是常态不是故障。管线为此专门做了可自证：`vision-audit.jsonl` 记录全量（含落选与失败），`vision_zero_image_failure(stats)` 是 ERROR 日志与 TG 告警**共用的单一判定**，防止两个口径分叉说不同的话。

### 推送阶梯

```
citation_map 有图 且 img_relay 开启
  ├─ 成功 → sendRichMessage 单条图文混排（正文 + 内嵌图）
  └─ 任一步失败 ↓
回落：tg.send(全文一条，支持断点续传) + 尾随图片逐张独立发送
```

富消息用 Bot API 10.x 的 `sendRichMessage`（32768 字符 + 最多 50 个媒体块）。媒体自 Bot API 10.2 起走**多部分表单直传**（tg_sender.send_rich_message(media=…)），不再经 Cloudflare KV 公网中转；KV 中转模块（img_relay.py）已退役、仅旧 config 兼容保留。

回落路径用 `state_path=.text-push-state.json` 支持多 chunk 断点续传；富消息路径没有 chunk 级续传，所以幂等粒度是 day-level marker。

## 频道转发管线

选定频道的消息逐条原样转卡片，**完全跳过 LLM**。

**公开 vs 私有走两条路**，由 `RawChannel.username` 决定：

- **公开频道**：`t.me/<username>/<msg_id>` 链接 + `link_preview_options` 富预览 + inline「打开原文」按钮。读 tg-cli 的 `messages.db`（纯文本）。
- **私有频道**：没有公开链接，Telegram 给不出预览。改用登录 session 下载媒体，经 bot 重新上传，媒体推送后立即 `shutil.rmtree`（文字已投递、媒体可重抓，不留二进制）。

**增量靠 msg_id 高水位**（`SeenStore.max_msg_id`），公开走 SQL `AND msg_id > ?`，私有走 telethon `iter_messages(min_id=)`。这是每 2 小时能跑的前提——否则高产私有频道每轮重下当天全部媒体，会撞 600s 子进程超时。

**相册折叠是推断出来的，不是读 `grouped_id`。** 私有路径能拿到 `grouped_id`，但公开路径读的 `messages.db` 里 `raw_json` 全表为空。所以公开路径改用"**空正文 + msg_id 连续 + 时间戳在 10s 窗口内**"推断相册成员，折进上一条卡片。规则保守：任何带文字的消息都另起一帖，保证两条真实文本帖永不被合并。

**相册的每个成员 id 都要写 seen**，不只 head——只记 head 会让高水位卡在相册首条，下次重新抓到尾部媒体、当占位卡再推一次。

## 成长挖掘管线

从指定群聊挖个人成长素材，A/B 择优推送。

```
mine_day(target_day)  → 切片入队（天级幂等，重跑快速返回）
  ↓ 部分失败 → 好 chunk 已入队，当天不标记 mined，次日 catch-up 重挖，不阻塞发卡
pick_next(prefer_date)
  ↓
build_card_a（确定性模板，零捏造风险）
build_card_b（LLM 叙事）
  ↓
judge(judge_llm, A, B, rubric) ← 异源：A/B 由 deepseek 写，judge 用 grok-4.5
  ↓ 任一步异常 → 回落 A 卡
发送 → mark_sent（daily_quota 门控）
```

**judge 异源是方法学决定**：B 卡作者与评审同为一个模型时存在自评自偏好。`Growth.judge_model` 设空即回落同源，日卡永不因 judge 配置断供。

**溯源只靠本地切片。** 源群消息一天一清，所以 `t.me` 深链对任何账号 24h 内必成死链——卡片上的跳转按钮已整个删除，`~/chat-daily/growth/segments/` 的本地切片是原文的**唯一长期载体**，`INDEX.md` 由 DB 全量重建（幂等）提供快查。

**金句必须逐字。** 展示文本从 DB 逐字反查片段，不用 LLM 排版版；零有效金句的段落降级 rejected（无可信锚点即不推送）。

## B站 digest 管线

已迁 r4s，代码仍在本仓库。

**双 transport**：`api`（默认，medialist + view 两个接口，零 cookie 零 WBI 签名，单 UP 一次调用拿齐字段）和 `opencli`（fallback，走 Chrome bridge）。`arc/search` 会 -352 风控，已弃用。

**B站请求必须直连**（`trust_env=False`，含 hdslb 封面 CDN）——海外出口即风控。这与同一台机器上 TG/Gemini 走 bwg tinyproxy 出海的需求直接冲突，所以是两套 client 而不是一套。

-352 是 IP 级判决，首个 UP 命中即中止本轮（降频不绕过），不对已风控的 IP 连打 22 次。

## 横切关注点

### 幂等：day-level 阶段 marker

归档目录下的 marker 文件构成状态机，**不做内容 hash**——catch-up 重跑会生成不同文本，但"当天这个阶段最多送达一次"与内容无关：

| marker | 守护 | 特别之处 |
|---|---|---|
| `.persisted` | 机会入库 | 挡 catch-up 重复 append 非确定 id 的 hot_leads |
| `.card-sent` | PNG 卡片 | 存在 `.digest-sent` 时抑制迟到卡片（保 card-first 契约） |
| `.digest-sent` | 日报正文 | 写在尾随照片**之前**——正文绝不重发，代价是崩溃时不补图 |
| `.run-complete` | 整轮 | **仅 push 成功才写**；`--no-push` 调试跑不得抑制补跑 |
| `.text-push-state.json` | 多 chunk 续传 | 按 payload-hash，内容变了则整发 |

频道转发与 B站的幂等不用 marker，用 `SeenStore`（append-only 文件，**发后才写**，key 为 `chat_id:msg_id` / `bilibili:<bvid>`）。

### 数据层

`~/chat-daily/chat-daily.db` 单文件 SQLite（WAL + `synchronous=NORMAL` + `busy_timeout=5000`），2026-06-29 从 JSONL 迁来。每次操作开短连接——低频管道，不需要连接池。

`permanent.fingerprint` 有 UNIQUE 约束；算指纹前先 `_canonical_url()` 剥离 utm_*/from/share*/spm/fbclid 等跟踪参数和 fragment，避免同一链接因分享渠道不同而重复入库。

`permanent.md` 和 `hot-leads/latest.md` 是**从 DB 每轮重生成的派生视图**，不是事实源。

### LLM 信任边界

这是全项目反复出现的教训，已升为 `CLAUDE.md` 的硬规则：**LLM 产出的结构必须有 code-level 兜底，prompt 约束只作辅助。**

现存兜底层：

| 位置 | 挡什么 |
|---|---|
| `_extract_fences` | fence 解析（行级状态机 + 已知顶层 opener 硬边界）；正文里的代码块不再截断解析 |
| `coerce_enum` | 枚举字段漂移 |
| `_normalize_score` | 分制漂移（0-10 制 / 百分制 / bool 全归一到 0-1） |
| `resolve_citations` | `[IMGn]` 重复标记去重、未知编号剥除 |
| `_best_effort_summary` | verifier 与 repair 双双解析失败时的尽力提取 |

分层降级的设计目标是**永不整天零产出**：verifier 解析失败 → 发 initial 草稿；repair 也失败 → 尽力提取 concise；只有连 concise 都没有才 raise。

### 网络与代理拓扑

三种出口需求在同一台机器上冲突，所以按目标分流：

| 目标 | 走法 |
|---|---|
| CLIProxyAPI（`127.0.0.1:8317`） | 直连，`NO_PROXY` 必须放行 |
| TG / Gemini | http 代理（Mac 本机 1082；r4s 经 bwg tinyproxy over tailscale） |
| B站（含 hdslb CDN） | **强制直连**，`trust_env=False` |

`ALL_PROXY` 是这套拓扑的头号杀手：venv 的 httpx 无 socksio extra，带 socks5 的 `ALL_PROXY` 会让 `httpx.Client()` **构造即抛 ImportError**，在 `NO_PROXY` 求值之前——2026-07-03 曾一次打挂三条管线。修法是 `run_daily.py` `__main__` 第一行 `scrub_socks_proxy_env()`，一处覆盖三个 launchd 任务 + 手动运行 + 全部子进程。

### 失败与告警

原则是**投递优先于完美**：每个增强阶段（卡片、图片、富消息、持久化、派生视图）都 try/except 包裹并有回落，任何一步失败都不得阻塞正文送达。

告警经 `notify_failure`（osascript + TG alert 话题），由 `CHAT_DAILY_TG_ALERTS` 门控——测试和即兴运行不触发，guarded wrapper 置 1。标题与正文先经 `redact()` 脱敏再发。

有意保留的告警冗余：in-Python 优雅失败发一条，wrapper 捕获非零退出再发一条，同一故障可能收到 2 条 TG。视为安全冗余，未消除。
