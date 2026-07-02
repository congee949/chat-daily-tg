# Implementation Notes

## Design Decisions

- Evidence indexing is narrowed at chunk-selection time: keep high-risk messages and adjacent informative context, instead of embedding every exported chat line.
- Fact-risk reporting is generated from verifier JSON after the verified summary is parsed, so it reflects the final verifier decision rather than the draft.

## Deviations

## Tradeoffs

- Adjacent short context is kept when it sits next to a high-risk message, because terse chat replies like “这个能直接读x” can be crucial evidence even though they are short.

## Open Questions

---

## 2026-06-02 — Push audit fixes + image output

### Design Decisions
- **Image output** is an *add-on*, not a replacement: `run_daily` sends the PNG card first (when `telegram.send_image` is true, default **false**), then ALWAYS sends the full HTML text — so the proven text path is the fallback and no information is lost. Gated behind a config flag so behavior is unchanged until opted in.
- **Card renderer toolchain**: system `Google Chrome --headless=new --screenshot` + plain f-string HTML, chosen over Jinja2/Playwright because the `.venv` has neither package — the Chrome-subprocess path adds zero Python deps and is byte-deterministic/offline, which suits the unattended launchd run. `card_renderer.parse_concise_to_card` reuses the fixed `### emoji` heading schema from `prompts.py` as its parser contract.
- **SEC-1 log redaction** done at the *formatter* level (`_RedactingFormatter`) so it scrubs both the message and the exception traceback (where the httpx error embeds the bot-token URL). Regex has no `\b` anchors because the token appears as `.../bot<digits>:.../` — a leading word boundary never matches there.
- **SEC-2**: secrets are now `.env`-only. Stripped `DEEPSEEK_API_KEY`/`TG_BOT_TOKEN`/`TG_CHAT_ID` from both the installed launchd plist and the tracked template (values were verified identical to `.env` first), keeping only `PATH`. `run_daily` already calls `load_env_file(DATA_DIR/.env)`, verified to supply all secrets in a clean env. Reloaded the agent.

### Deviations
- `_send_one` no longer formats; formatting moved up into `send()` so chunking happens on the FINAL payload (CHUNK-1). Split limit lowered 4096→3900 for margin. Existing tests still pass because net formatted output is unchanged.

### Open Questions
- Caption is a plain-text teaser (first overview line). If a richer caption is wanted later, it must avoid truncating an open `<b>` tag — keep it plain text.
- `deploy.sh` still has a label mismatch (`com.chat-daily.tg` vs real `com.chat-daily-tg.agent`) so it currently no-ops on the plist; now that the template carries no secrets it is safe to fix the label, but that was out of scope for this pass.

---

## 2026-06-06 — 频道原文卡片（raw channel cards）

新增能力：选定频道的消息**逐条原样**推成 X-Monitor 式卡片，**完全跳过 LLM 总结**。

### Design Decisions
- **独立 stage，绝不污染总结**：在 `run_daily._run` 总结推送之后调 `_push_raw_channels`，整段 try/except 包裹 —— 卡片阶段任何异常只 `log.exception` + `notify_failure`，已发出的每日总结不受影响（总结先发，卡片后发）。
- **公开 vs 私有**：`RawChannel.username` 决定走向。有 username → `t.me/<username>/<msg_id>` 链接 + `link_preview_options`（富预览卡）+ inline「打开原文」按钮（复刻 X Monitor `send_telegram`）；无 username（私有频道）→ 纯文字、无预览、无按钮。
- **媒体空文本消息**：公开频道即便正文为空也推卡（占位文本 `🖼 …`，靠链接预览展示媒体）；私有频道空文本则跳过（既无文本又无可预览链接，无意义）。用户选了"全文逐条原样/不限量"，所以**不套用** `should_skip_content` 的降噪过滤（那是给 LLM 用的）。
- **send_card 走 JSON body 不走 form**：`link_preview_options`/`reply_markup` 是嵌套结构，新增 `_post_json` 用 `httpx json=`；保留旧 `_send_one`（form）不动，总结路径零改动（surgical）。
- **限流**：`raw_card_delay_seconds`（默认 1.0s）每卡间隔；`_post_json` honor 429 `retry_after`、5xx 退避、400 一次性降级为纯文字重发（单条坏消息不拖垮整批）。用户要求"不限量全部逐条推"，高产频道会刷屏但按退避走、不绕过限流。

### Deviations
- 选定的 7 个频道里 **6/10 号（私有频道A、私有频道B）为私有** → 自动退化纯文字（已与用户确认可接受）。

### Tradeoffs
- 卡片阶段挂在总结成功路径里：若 `groups_with_content` 为空导致总结提前 `return 1`，卡片也不会发。当前总结群几乎总有内容，按最小改动接受此耦合，未独立成顶层 stage。

### Open Questions
- 网络：生产 launchd 仅注入 PATH、无代理变量，依赖 Shadowrocket TUN 透明代理直连 api.telegram.org（与现有总结推送同路径，故卡片无新增网络风险）。本机交互式 shell 里 `ALL_PROXY=socks5` 会让 httpx 因缺 socksio 报错——仅影响手动测试，需 `env -u ALL_PROXY -u all_proxy`（保留 http 的 HTTPS_PROXY）。

---

## 2026-06-06（续）— 私有频道：下载媒体 + 打开原文链接

私有频道无 `t.me/<username>` 公开链接 → Telegram 给不出预览卡。改为：用登录用户 session 下载消息媒体，经 bot 重新上传，连同全文 + `t.me/c/<内部ID>/<msgid>` 打开原文链接一起发。

### Design Decisions
- **下载用 subprocess 调 kabi-tg-cli 解释器**（`scripts/tg_media_dump.py` 用 telethon + 现有 session），chat-daily-tg 的 `.venv` 保持无 telethon 依赖——延续项目既有"shell out 到 tg"的架构。解释器路径 `TG_CLI_PYTHON` 默认 `~/.local/share/uv/tools/kabi-tg-cli/bin/python`，可用 env `CHAT_DAILY_TG_CLI_PYTHON` 覆盖。
- **打开原文用 HTML 文本链接而非 inline 按钮**：媒体相册（sendMediaGroup）不支持 reply_markup，为统一 text/单图/相册三条路径，一律把 `🔗 <a>打开原文</a>` 写进文本/caption。`t.me/c` 链接在 Telegram 内对成员可点开（用户是成员）。公开频道仍走预览卡、无链接行（避免冗余）。
- **文本与媒体分发**：有文本 → 先发整段文本消息（含链接行，关预览），媒体紧随其后裸发（无 caption），从而**全文不受 1024 caption 上限限制**；纯媒体 → caption=header+链接行（header 短，必然 < 1024）。
- **相册类型混装防 400**：全为 photo/video → 一个 media group；混入 document/audio → 回退逐条发（caption 落在第一条）。单文件 → sendPhoto/sendVideo/sendAudio/sendDocument 按 mime 选。
- **大小上限**：下载脚本跳过 >45MB 的文件（bot 上传上限 ~50MB），避免浪费。

### Tradeoffs
- 私有频道一条"文本+媒体"帖会产生 2 条 TG 消息（文本 + 媒体）。换取"全文不截断 + 媒体随行"，可接受。
- 高产私有频道（私有频道A）每日下载+重传媒体较重；用户已选"不限量"，按 `raw_card_delay_seconds` 间隔 + 429 退避，不绕过限流。

### 验证
- 单测 154 passed（新增 `test_private_media.py`：相册合并、send_media/send_media_group multipart）。
- 实测 私有频道B（私有）下载照片+文字+链接经 bot 推送成功（pushed=1）。

### 修订（同日，用户反馈）
- **图文合并为单条**：私有频道把全文作为媒体 caption 一起发，不再"文本一条 + 媒体一条"。`len(body)<=1024` 时单条合并（单图/相册 caption 落首图）；仅当全文超 1024（Telegram caption 硬上限）才回退"文本消息 + 裸媒体"。
- **删除打开原文链接**：用户非该频道成员，`t.me/c` 链接对其无效 → 私有频道不再附 `🔗 打开原文`。`internal_channel_id`/`tme_c_link` 及对应测试一并移除。

---

## 2026-06-06 — 多智能体流程审查 + 修复

用 6 维度 workflow 并行审查频道推送流程并对抗式复核（13 confirmed / 3 false-positive / 1 uncertain）。已修复的 confirmed 项：

### Design Decisions / Fixes
- **429 无界重试（HIGH）**：`_post_json`/`send_media`/`send_media_group` 在 429 时 `continue` 不增 `attempts`，持续 429 会死循环挂起整个 launchd run。改为独立 `rl_hits` 计数，达 `retry_max_attempts` 即 `raise RuntimeError`，由 per-message/per-channel handler 跳过。
- **私有 dump 窗口被今天消息吃掉（HIGH）**：`iter_messages(limit=)` 从最新开始，今天的消息（窗口之后）也消耗 limit，高产频道会在到达昨天前耗尽 → 抓不到目标日。加 `offset_date=end`，让 limit 用在窗口内及更早消息。
- **无幂等（HIGH）**：新增 `raw_seen.SeenStore`（append-only 文件，key=`chat_id:msg_id`，**发后写入**）。公开按 msg_id、私有按 post.first_msg_id 去重。重跑/补跑/重试不再重复刷屏。实测 RUN1=1、RUN2=0。
- **公开 `read_messages` 丢最新（MEDIUM）**：`ORDER BY ASC LIMIT` 在高产日丢弃最新消息。改为内层 `DESC LIMIT` 取最新 N 再外层 `ASC` 渲染；命中 limit 时 `log.warning`。（共享函数，输出顺序不变，仅改"超限时保留哪批"。）
- **媒体磁盘无限增长（MEDIUM）**：私有频道下载的媒体推送后 `shutil.rmtree(out_dir)`（finally 内），文字已投递、媒体可下次重抓，不留二进制。
- **总结无内容则频道被跳过（MEDIUM）**：频道卡片是独立内容，不应因总结源（群A/群B）当天无料而被早退跳过。`_push_raw_channels` 改为自建 bot sender，并用 `_do_raw()` 一次性 guard，在两处早退 `return 1` 与正常结尾都恰好执行一次。
- **config 校验忽略 raw_channels（MEDIUM）**：`normalize_sources` 的 `has_telegram` 加入 `raw_channels`，避免将来清空 chats、只留频道时启动报错。
- **`--no-push` 仍下载私有媒体（LOW）**：`push_private_channel` 在 `no_push` 时 dump 前即 return，干跑保持轻量。
- **build_card 单行异常拖垮整频道（MEDIUM）**：公开 card 改为逐行 try/except，坏行（如坏时间戳）只跳自己。
- **混合相册整帖回滚（MEDIUM）**：`_send_media` 混类型逐条发时 per-item try/except，仅当全部失败才 raise（≥1 成功即保留）。

### 误报（复核判否，未改）
- 相册 >10 静默截断：实际 dump 每消息只 1 媒体、相册按 grouped_id 累积，Telegram 相册本就 ≤10，`items[:10]` 永不触发。
- subprocess stdout 被库输出污染：tg_cli console 是 `stderr=True`，所有提示走 stderr，stdout 是干净 JSON。
- `caption[:1024]` 切坏 HTML 标签/实体：合并路径已被 `len(body)<=1024` 闸门挡住，slice 是 no-op；且 escape_html 只产生良构实体。

### 实跑发现并修复（2026-06-06 首次真实补推）
- **私有 dump 单文件下载可卡死整频道**：首跑预检时「私有频道A」dump 超 600s 子进程超时（实际仅 21 条/10 媒体，是某文件下载卡住）。`tg_media_dump.py` 给每个 `download_media` 包 `asyncio.wait_for(timeout=45)`，慢/失败文件跳过当文字，单文件不再拖垮整批。修复后该频道正常推完。
- **首次启动方式**：今早 06:30 已用旧代码推过 06-05 总结，故只补推 06-05 频道卡片（不重跑总结）。实推 24 条（公开2+私有频道B1+私有频道A21），生成真实 seen 文件，立即重跑验证为 0（幂等）。

### 频道宣传行剔除（用户反馈：删除「示例频道·备用频道·投稿通道」）
- 抓样确认「私有频道A」每条结尾固定一行 `🌸 示例频道 · 备用频道 · 投稿通道`（开头无样板，正文直接是标题）。
- 新增 `RawChannel.strip_patterns`（regex 列表）+ `strip_promo_lines()`：按整行 regex search 剔除匹配行、合并空行、trim。公开走 `build_card`、私有走 `private_media` 均应用。
- config.yaml 给私有频道A配 `['示例频道','备用频道','投稿通道','投稿频道']`。私有分支用**剔除后**的 `text` 判定分支（promo-only 消息剔空后若无媒体则跳过）。
- 无 patterns 时是 no-op，不影响其它频道。已发的 24 条历史卡片未改（bot message-id 未持久化，无法 API 编辑/删除）。

### 保留新闻来源链接（用户反馈：尾部要带来源链接）
- 抓 entities 确认：来源词（Bloomberg/CBS/证券时报网/Reuters）是带隐藏 URL 的 `MessageEntityTextUrl`；尾部 花频道/用频道/稿通道 是指向频道自身 t.me 的推广链接。纯文本 `m.message` 把所有 text-link 的 URL 都丢了 → 来源不可点。
- `tg_media_dump.py` 改为额外输出 `html = telethon.extensions.html.unparse(text, entities)`（Telegram 兼容 HTML，含 `<a>` 来源链接 + `<strong>` 标题）。
- 新增 `strip_promo_lines_html()` + `visible_text()`：按**可见文字**（去标签）整行剔除推广行——尾行连同其频道链接一起删，但来源行的 `<a href>` 保留。私有渲染改用 `Post.html`，caption 长度闸门改用 `visible_text` 计可见长度（HTML 标签不计入 Telegram 1024）。
- 公开频道路径不变（仍纯文本 + 预览卡；用户只问私有私有频道A）。私有频道B等私有频道顺带也用 HTML（其正文明文 URL 本就在文本里，无害增强）。
- 已对 06-05 示例频道 21 条**重推带链接版**（先从 seen 删该频道条目再单独重推；其它频道不动），用户删旧无链接批。

## 2026-06-06 — 频道改为 2 小时增量转发（与每日总结解耦）

用户要求频道内容从"每日一次"改为"每 2 小时一次"（白天 08–22），每日 LLM 总结保持 06:30。

### Design Decisions
- **解耦为两个 launchd agent**：`com.chat-daily-tg.agent`（06:30，**只做总结**，已从 `_run` 移除频道阶段 `_do_raw`）；新增 `com.chat-daily-tg.channels`（08/10/.../22，跑 `run_daily.py --channels-only`，**只做频道转发**）。
- **新入口 `run_channels()`**（`--channels-only`）：不跑总结，窗口 `[今天-1, 今天+1)` 作安全边界，真正的过滤靠高水位。
- **增量（high-water-mark）是关键**：`SeenStore.max_msg_id(chat_id)` 给出每频道已推的最大 msg_id；公开走 `read_messages(min_msg_id=hwm)`（SQL `AND msg_id>?`），私有走 `dump_channel(min_id=hwm)`→telethon `iter_messages(min_id=)`。这样**高产私有频道每 2h 只抓/下载新消息**，不重复下载当天旧媒体（否则 600s dump 超时）。`push_raw_channel_cards(incremental=True)` 触发。
- **夜间不打扰**：08–22 共 8 次；夜间(22→次日08)消息在 08:00 首跑按 hwm 补推。
- 日志单独写 `channels-<date>.log`，与总结日志分离。

### 验证
- 单测 164 passed（新增 max_msg_id、read_messages min_msg_id 增量过滤）。
- 实跑 `--channels-only`：私有频道A只抓今日 7 条新（**未重抓昨日 21 条/媒体**，证明增量生效），共推 8 条；立即再扫推 0（幂等）。两个 agent 均 loaded。

### Open Questions / 已知保留（低优先）
- `send_media`/`send_media_group` 无 `_post_json` 那样的 400→纯文本降级（uncertain）：因 caption 良构且 ≤1024，实战触发概率低，留作防御项未加。
- `send_card` 多分块中途失败会留半条（LOW，narrow）：隔离尽力阶段，暂仅日志可见。
- seen 文件 append-only 长期增长（每行~20B，可接受）；未来可按日期前缀裁剪。
- run_daily docstring/config.yaml 写 08:00 但 launchd 实际 06:30（pre-existing 文档不一致，未在本次范围内改）。

## 2026-06-10 — 晨跑失败根因修复 + embedding/verifier 提速 + 睡眠免疫调度

06-10 06:30 run 失败（日报未发）。根因：MacBook 合盖电池睡眠——请求发出 2s 后入睡，43.5min 墙钟里进程仅实际运行 ~80s（600s timeout 没机会走完），DarkWake 醒来发现代理 TCP 已被对端断开 → `RemoteProtocolError`，而旧重试网 `(HTTPStatusError, TimeoutException, ConnectError)` 不含 `ProtocolError` 分支，一次击穿。

### Design Decisions
- **重试网扩为 `(HTTPStatusError, TransportError)`**（llm_client）：TransportError 统一涵盖 Timeout/Connect/Read/Proxy/Protocol；config 的 600s timeout 本身正确，不改。
- **embedding batch 10→100**：`batchEmbedContents` 实际上限 100/请求（官方文档不写明，langchain-google-genai 写死 100）；常态 ~50 chunks 从 6 请求+80s 睡眠变 1 请求 0 睡眠。请求数下降对 RPM 更友好，16s inter-batch delay 保留（仅 >100 chunks 生效）。claim query embedding 同样合并为单请求（原 ≤12 次串行）。
- **verifier 跳过条件 = builder 返回空串**：空串 ⟺ `extract_claim_queries` 零命中（有 query 必有 "### Claim 查询" 头），即 verifier 的核验清单为空 → 跳过第二次 LLM 调用，`verification={"checked_claims":[]}`（写 verification.json，与"没跑"可区分）。embedding 禁用（builder=None）时保持 verifier 必跑——无信号不敢跳。
- **调度睡眠免疫 = caffeinate + 当日补跑**：`caffeinate -is` 防 run 中途 idle/AC 睡眠，但**防不了电池合盖**；兜底靠 9:00/13:00 两个 catch-up interval + `--skip-if-done`。成功标记 `.run-complete` 写在 archive 日目录，**仅 push 成功才写**（--no-push 调试跑不算交付，不得抑制补跑）。launchd 同 label 不并发、睡过的触发点唤醒时合并，无需锁。
- **群B limit 500→1500（config.yaml）**：limit 同时控制 `tg sync -n` 抓取深度和 SQL 读取上限，sync 抓不到的消息 SQL 分页也救不回，调一个旋钮覆盖两层。

### Tradeoffs
- catch-up 选 9:00/13:00 两次：覆盖"早晨没醒/上游故障"两类，再多收益递减（launchd 唤醒合并已兜长睡）。
- 跨天 embedding 缓存不做：batch=100 后整步只剩 ~3s 单请求，ROI≈0。
- **替代唤醒方案均否决**：`pmset repeat wakeorpoweron`（RTC 定时唤醒）需 root 写系统级持久状态（仓库/deploy 不可控），且电池合盖下唤醒只是 dark wake，撑不住 20+ 分钟的 run，盲区照旧，还添每日强制唤醒的电量开销；`caffeinate -u`（用户活动断言促 full wake）会点亮屏幕，06:30 无人值守不可接受；Power Nap / wake-on-demand 的 dark-wake 窗口时长与调度由系统支配、无法按 job 粒度控制——本次故障恰是在这种窗口里跑出来的。

### 验证
- 169 passed（含 RemoteProtocolError 重试回归、大 batch 切分、verifier 跳过、marker 写入/跳过）；tests/conftest.py 全局清代理变量后，带 ALL_PROXY 的 shell 直跑不再假红。
- 实跑补发 06-09 日报成功：23 chunks embedding 16min→2.1s，全程 ~105s（原 ~3.5min）。
- launchctl 重载后 `launchctl print` 确认 3 个 calendar interval + caffeinate 参数生效；`--skip-if-done` 冒烟实测秒退 exit=0。

### Open Questions / 已知保留
- summary 失败后 export/embedding 成果不复用（补跑全重做）：catch-up 机制下可接受，未加断点续跑。
- deploy.sh 与现状脱节且危险（label 写错 `com.chat-daily.tg`、`git reset --hard` 会清掉未提交工作、pip 而非 uv）：本次未动，提醒勿直接运行。
- `retrieve_evidence_for_text` 已无仓库内调用方，待在途修改合并后可删。

---

## 2026-06-11 — Telegram 图片接入 vision（绕过 tg-cli 无媒体）

### Design Decisions
- **补充式旁路，不改 telegram_exporter**：核查确认 tg-cli(kabi-tg-cli 0.6.0) 架构上只存文本——源码 0 处 media/photo 处理、messages.db 的 raw_json 全 NULL、CLI 无 `--media` 选项、content 连 `[图片]` 标记都没有，图在 sync 即丢。故新增 `telegram_media.export_chat_media` 作图片旁路，复用 channel forwarder 已有的 `private_media.dump_channel`(telethon 直连下载，借 kabi-tg-cli 解释器+登录 session)，把照片转 MediaCandidate 喂现成 vision pipeline；经验证的 tg-cli 文本链路不动。
- **gate 在 vision.enabled**：下图唯一目的是喂 vision，关则不下、省 telethon 拉取。接入点在 run_daily TG 导出循环内，每 chat 文本导出后补下图，失败返回 [] 不挡文本日报。
- **只保留 photo**：tg_media_dump 也下 video/audio/document，但 vision prompt 针对静态图，其余跳过。
- **vision 后端 = 本地 qwenproxy**：config.models.vision 指 `http://localhost:3000/v1` + qwen3.7-plus（OpenAI 兼容，Playwright 驱动 chat.qwen.ai，本地免费、零代码）。实测单图 ~50s、返回 schema 完美匹配 VisionAnalysis。

### Tradeoffs
- **下载图 score 保底 0.5**：media.py prefilter(>=0.45) 按微信聊天关键词打分，频道/群图 caption 几乎不命中 → 多数图被滤、vision 看不到。既已付下载成本，保底让其过 prefilter，由 vision 的 value_score postfilter(>=0.65) 做真正筛选。实测电丸 4 张：银行截图 0.0 / Codex 额度 0.2 / NotebookLM logo 0.1 / PDF 分享 2.5——筛选方向正确，隐私/闲聊图被滤。
- **每群 max_photos=20 安全阀**：vision ~50s/图串行，高 limit 群(电丸 1500)的图多日可拖垮无人值守 run；溢出取最近 N 张。常态每天 3-5 张几乎不触发，仅防极端。

### Open Questions / 已知保留
- **value_score 范围不稳**：qwen 偶返 >1（PDF 分享判 2.5）；现有 analyze_media_candidates 用固定阈值 0.65 且忽略 `should_include_in_daily` 字段，是 master 既有筛选逻辑的脆弱点，本次未动（超出"接 telethon"范围）。
- **首测 6:30**（非 8:00）：日报源仅 TG(微信暂停)，电丸有图(~2-3/天)、CuiMao 少；图片理解段可能 0-2 条，是 vision 正常筛选结果而非故障。
- telethon 下图每群一次 subprocess(timeout 600s)串行 + vision 串行，图多日 vision 段可达 10+ 分钟，靠 caffeinate 兜睡眠。

### 验证
- 178 passed（新增 test_telegram_media 4：photo 过滤/score 保底/max_photos 截断/失败降级）。
- 端到端实跑：电丸群 telethon 下 8 张真图(59s) → vision pipeline → 低价值图正确滤掉、链路通；单图理解准确（持仓截图读对全部数字、判不入日报）。
- launchd 确认跑工作树 repo+venv(import 指向已改 src)；config 开 vision 指 qwenproxy、.env 加 VISION_API_KEY。

---

## 2026-06-23 — 公开频道相册折叠（修「一帖推很多次」）

现象：yihong0618 频道 18:47「一条帖子」在 bot 里推成 5 张卡片（1 张正文 + 4 张 `🖼 媒体内容` 占位）。

### Design Decisions
- **根因不是去重坏了**：那条 18:47 是一个 Telegram 相册（media group），Telegram 存成 5 条独立消息（msg_id 13545–13549，同一秒）。msg_id 去重完全正常（每条只推一次），但**推送粒度是"消息"而非"帖子"** → 一个相册 = N 张卡片。
- **相册靠推断而非 `grouped_id`**：`private_media.py` 私有路径已按 `grouped_id` 折叠相册，但公开路径读 tg-cli 的 `messages.db`，其 `raw_json` **全表为空**（实测 0/42713 行有值），拿不到 `grouped_id`。改用库里现有信号推断：**空正文 + msg_id 连续 + 时间戳在 10s 窗口内** 的消息折进上一条的卡片（`_group_albums` + `_within_album_window`）。
- **折叠规则保守**：只有空正文项折叠；任何带文字的消息都另起一帖。这保证两条真实文本帖永不被合并（即便 id 连续紧挨）；10s 窗口拦住"id 连续但隔了几分钟"的独立媒体帖。
- **相册每个成员 id 都写 seen，不只 head**：照搬 `private_media.py` 的注记——只记 head 会让增量高水位 `max_msg_id` 卡在相册首条，下次跑重新抓到尾部媒体并当占位卡再推一次。

### Tradeoffs
- **caption 在末条的相册**（Telegram 允许 caption 落在任一成员；实测 yihong 数据均在首条）会拆成"占位相册卡 + 文本卡"两张，而非一张。可接受：罕见，且仍远好于 N 张；无真实样例前不加额外逻辑。

### 验证
- 188 passed（新增 5 个测试：相册折叠/真实文本帖不合并/超窗独立/纯媒体相册折叠/push 一卡且全 id 入 seen + 幂等重跑 0）。
- 实跑真实数据：yihong 06-23 窗口 7 条原始消息 → 3 张卡片（18:47 的 5 条相册 → 1；47 分钟后的独立媒体帖 13551 正确保持独立）。
- 部署：launchd 直跑仓库源码 `run_daily.py`，无需重装，下次 22:00 频道跑自动生效；历史已推 13545–13551 全在 seen，不会回头重推。

---

## 2026-07-02 — 微信图片下载接入 wx extract + 媒体保留期清理

用户发现"每天获取的内容有没有多媒体"这个问题的答案比预想的更极端：核对 `media_candidates.jsonl` 实测数据后确认，微信侧图片候选 **100% `local_path=None`**——`extract_wx_media_candidates` 从未真正下载过图片，只是从文本里解析 `local_id` 占位符打分，vision 的 `not candidate.local_path` 前置过滤直接把所有微信图片挡在外面。同时发现 Telegram 侧 `tg_media/` 已无清理地堆了 1.3G（20 天），是因为 `telegram_media.export_chat_media` 不分打分先无条件全下载。

### Design Decisions
- **微信下载走"先打分后下载"，不照搬 Telegram 的"先下载后筛选"**：`wx attachments --json` 返回的 `local_id` 字段与导出文本里的 `local_id=NNN` 是同一 ID（实测核对过），因此可以在拿到图片文件之前，先用现成的 `score_media_context`（纯文本关键词打分，媒体不到场也能算）筛出 score≥0.45（与 `vision.py` 的 `min_prefilter_score` 对齐，不留二次过滤的缝隙），只对达标的候选调 `wx attachments`（每群一次，取 local_id→attachment_id 映射）+ `wx extract`（按需逐张）。今日样本验证：76 条微信图片候选里只有 13 条（17%）达标，量级远低于 Telegram 现在"全下载"的做法。
- **落盘位置与命名对齐 Telegram 既有约定**：`archive/日期/wx_media/<safe_filename(group_name)>/<local_id>.jpg`，与 `tg_media/<chat_name>/<msg_id>.jpg` 同构，`cleanup_old_media` 可以用同一套逻辑处理两者。
- **提取幂等 + 单项失败隔离**：`out_path.exists()` 时跳过重复解密；单张 `wx extract` 非零退出只 `log.warning` 并保留 `local_path=None`，不让一张坏图拖垮整个 `export_group`。
- **保留期清理是独立的、覆盖两条路径的通用函数**：新增 `archive.cleanup_old_media(retention_days)`，只删 `tg_media`/`wx_media` 子目录（媒体可重新下载），不动同目录下的 `summary.md`/`vision.jsonl` 等文本归档（这些量小、是真正的永久记录）。默认 14 天，与 `HotLeads.retention_days` 现有默认对齐，新增 `Archive.media_retention_days` 配置项。在 `_run`（每日总结入口）顶部调用，try/except 包裹、失败仅 `log.warning`，不得阻塞日报——`--channels-only`/`--bilibili-only` 两个独立入口不调用它，因为频道转发媒体本就在 `private_media.py` 里下载完即删（`finally: shutil.rmtree`），没有堆积问题。

### Tradeoffs
- **只支持图片**：`wx` CLI 的 `attachments --kind` 目前只接受 `image`，视频/语音/文件类消息仍然只有 `local_id` 占位、无法下载，这是底层 CLI 的能力边界，不在本次范围内。
- **打分先于下载，意味着"看不到图片内容"的打分可能误伤**：纯文本关键词打分无法识别"图片本身很有价值但配文很短"的情况（例如一张关键截图配文只有"看"）。Telegram 侧靠"全下载+vision 二次筛"能兜住这类情况，微信侧为了控制体积放弃了这个兜底。可接受，因为 vision 分析成本本身也是稀缺资源，且用户已经在"下载范围"的澄清问题里选择了"先打分再下载"。

### 验证
- 239 passed（新增 `test_wx_exporter.py` 3 例：高分候选下载成功/低分候选零 wx 调用/单张 extract 失败不中断；`test_archive.py` 3 例：清理过期媒体目录/保留近期/archive 目录不存在时 no-op；`test_config.py` 补 1 断言：`Archive.media_retention_days` 默认 14）。
- 未做真实环境端到端跑（未接触生产 `~/chat-daily` 配置/凭证），下一次日报（或手动 `--no-push` 跑一次过去日期）应确认 `media_candidates.jsonl` 里微信条目出现真实 `local_path`，且 `wx_media/` 目录按打分门槛只落高分图。

---

## 2026-07-02（续）— 每日推送里插图：LLM 主动引用 `[IMGn]`，按引用点插入图片

用户希望能在每日推送里实际看到高分图片，且要求"插在对应文字附近"而非"文字发完后堆一批图"，明确选择了较重的方案：让写总结的 LLM 自己判断某条重点是否有对应截图、在恰当位置插入引用标记，而不是靠启发式规则事后猜配对。

### Design Decisions
- **引用池 = vision 已筛选出的高分子集，不新增过滤层**：`analyze_media_candidates` 本就只返回 `value_score>=0.65` 的 `VisionAnalysis`，这正是用户要的"打分靠前的几张"。新增 `vision.build_citation_block(analyses)` 只是在这批基础上按 `value_score` 排序、封顶 `MAX_CITATIONS=5`（防止总结被图片刷屏），生成一个带编号的"可引用图片"markdown 块喂给总结 LLM，同时返回 `id_map={n: VisionAnalysis}` 供推送时反查。
- **标记走字面 token `[IMGn]`，不碰 parser**：`parse_summary_output` 按 fence 抽取，`[IMGn]` 只是 `concise` fence 里的普通文本，原样穿过既有解析逻辑，零改动。`post_process.py` 的 `_MD_LINK_RE` 只匹配 `[text](url)`（要求紧跟括号），`[IMGn]` 没有括号，不会被误处理。
- **拆分逻辑做成"文本+图 pair"而非"逐行插入"**：`vision.resolve_citations(text, id_map)` 两遍处理——先把不在 `id_map` 里的编号（LLM 幻觉）直接从文本抹掉但不产生断点，再按剩下的合法标记切分成 `[(text_chunk, image_or_None), ...]`；每个 `text_chunk` 与"结束这段文字的那个引用标记"对应的图片配对，最后一段（最后一个标记之后的文字）永远配 `None`。推送时对每个 pair 先 `tg.send(text_chunk)` 再 `tg.send_media(image, "photo")`，天然实现"图片紧跟它印证的那段文字"。
- **图片文件已提前下载好，引用只是"选哪张、放哪"**：不需要现场下载——vision 分析阶段用的 `candidate.local_path` 就是本地文件（微信走今天新接的 `wx extract`，Telegram 走 `telegram_media.py`），引用机制只做筛选和排版，不涉及新的下载路径。
- **resumability 只在"没有真正用到引用"时保留**：原来单条 `tg.send(..., state_path=...)` 支持同日补跑续传（review #42）。只要本次 push 真的产生了带图片的 segment，就切到逐段发送（无 `state_path`）；如果 vision 开了但 LLM 没引用任何图（`resolve_citations` 返回的所有 segment 图片都是 `None`），仍然走原来的单条可续传路径——用 `any(analysis for _, analysis in segments)` 判断，而不是简单地"citation_map 非空就切分支"（citation_map 非空只代表"有图可引用"，不代表"这次真的引用了"，用后者判断避免了大多数日子里因为 vision 恰好开着就无谓丢失续传能力）。

### Tradeoffs
- **多段推送没有逐段续传状态**：真正命中引用的那次 push，如果中途崩溃，同日补跑会全部重发（已知限制，写进代码注释而非工程化解决）。card 图片推送（`tg.send_photo` 发 PNG 卡片）今天也是同样没有续传状态，属于既有的不对称，未额外加码解决。
- **封顶 5 张引用池**：不是"vision 分析出的所有高分图都能被引用"，只暴露 value_score 最高的 5 张给 LLM 挑；vision.jsonl/vision.md 归档不受影响（仍是完整列表），只是可引用范围收窄。

### 验证
- 249 passed（新增 `test_vision.py` 6 例：`build_citation_block` 排序封顶/空输入；`resolve_citations` 合法标记切分/未知编号剔除不断段/无标记单段/local_path 缺失时丢弃标记；`test_run_daily.py` 新增 1 例端到端：LLM 输出里带 `[IMG1]`，断言 mock 的 httpx 层确实收到 1 次 `sendPhoto` + ≥1 次 `sendMessage`，且发出的文本里不残留 `[IMG1]` 字面量）。
- 未做真实环境验证：LLM 是否真的会按 prompt 指示恰当地引用图片（而不是从不引用，或引用位置很怪），这是模型行为问题，单测测不出来，需要至少跑一次真实 `--no-push`（vision 开启）观察 concise 输出。

### 修订（同日，三轮用户反馈后收敛为「全文一条 + 文末一图」）
用户对逐段插图的两版实推效果均不满意（V1 按引用点切分文字+独立图片消息 → 太碎；V2 图+段落文字合并为 caption 单条 → 每图仍是一条消息），核心诉求收敛为「日报尽量一条消息、图片放文末、最多一张、优先 AI 相关」。

- **单消息方案探索与否决**：(a) `sendPhoto` caption 上限 1024 可见字符，全文 ~2300 字符装不下；(b) 链接预览挂图（`link_preview_options.url`）**实测否决**——bot 先静默上传（`disable_notification`+发后即删）拿 `api.telegram.org/file/bot<token>/` 文件 URL 可公网拉取（200），但 Telegram 预览爬虫不渲染它（content-type 为 octet-stream；用户实看确认无图），且该 URL 内嵌 bot token 有转发泄漏风险；(c) 公共图床有公网 URL 但私群截图隐私不可接受。用户在「压缩日报到 1024 内图文真合一」与「保持信息量、接受两条消息」中选了后者。
- **最终形态**：`resolve_citations` 保留（默认 `max_images=1`，AI/工具 section 标记优先于文档顺序，多余标记从文字里剥离），但不再按标记位置切分推送——全文永远合成**一条完整文字消息**（`state_path` 断点续传因此恢复），选中的那张图作为**紧随的独立图片消息**（caption 仅来源行）。`_send_cited_segments`（caption 合并逻辑）整体删除，e2e 断言改为「1 条 sendMessage 全文 + 1 条 sendPhoto 尾图」。
- **prompt 同步**：`[IMGn]` 规则从「一条 bullet 最多 1 张」改为「全文最多 1 张，优先 AI/工具 相关截图」——标记位置只用于让 LLM 做语义挑图，不再影响排版。
- 251 passed；实推验证最终形态（全文一条 + Claude Code 截图尾随，演示时手动指定 IMG4，因当日 concise 是旧 3 标记 prompt 生成、AI section 恰无标记）。

### 再修订（同日晚，用户要求重查最新 Bot API 后彻底翻案：单条图文混排可行）
用户不满足于两条消息，要求重读 https://core.telegram.org/bots/api。抓取实时文档发现线上已是 **Bot API 10.1**（训练数据只到 ~9.0），10.x 新增 **sendRichMessage**：单条消息 32768 字符 + 最多 50 个媒体块（RichBlockPhoto 等）图文混排，支持论坛 topic——此前「Telegram 没有图文混排消息类型」的结论在新 API 下不成立。

- **媒体来源实测**（全部真调 API）：`attach://` ❌ `RICH_MESSAGE_PHOTO_URL_INVALID`；`file_id` ❌ 同错；`api.telegram.org/file/bot<token>/` URL ❌ `NO_MEDIA_FOUND`；纯 http/裸 IP ❌（访问日志为空，TG 根本不来抓）；**https + 正规域名公网 URL ✅**。TG 在 sendRichMessage 调用期间同步抓图并转存为自己的 PhotoSize（返回体可见），源 URL 发完即可销毁。
- **中转基础设施选型**：用户提供 bwg（美国 VPS）与 R4S（OpenWrt 主路由）。R4S 国内家宽+磁盘满直接排除；bwg 443 被 sui 面板占用、补域名/证书要动代理基础设施。最终改用**用户自己 CF 账号的 Worker + KV**（`tg-img-relay.g00094522.workers.dev`，本机 wrangler OAuth 过期→refresh_token 手动续期→REST API 部署）：随机 48 位 hex key、TTL 自动过期、发后即删、不经任何第三方图床。生产凭证为用户手工创建的**最小权限 API Token**（仅 Account/Workers KV Storage/Edit，永久有效，存 `.env` 的 `CF_KV_API_TOKEN`）。
- **代码落地**：新增 `img_relay.py`（KV upload/delete）、`TelegramSender.send_rich_message`（400 立即抛不重试→触发回退；429/传输错误按既有策略）、`run_daily._push_rich_digest`（用 RAW concise markdown 走 rich markdown 语法——不能用 post_process 后的 HTML 链接；`[IMGn]` 标记位置即图片插入位置，图文混排回归「引用点插图」的最初设想）、config 新增 `ImgRelay`。**任何一步失败都回退**到「全文一条+尾图一条」老路径，KV 清理放 finally。
- 257 passed；生产代码路径实推验证成功（完整 07-01 日报 + 内嵌 Claude Code 截图，单条消息）。

### Open Questions / 已知保留
- rich 消息在旧版 Telegram 客户端上的降级表现未验证（用户当前客户端渲染正常）。
- sendRichMessage 的 429/限流行为与普通消息是否同池未知，按同池假设处理。
- CF Worker `tg-img-relay` 与 KV namespace 属账号级长期资源，如弃用此功能需手动删除（dashboard → Workers & Pages / KV）。

### 三修（同日，放开 3 张 + 治「图糊」）
用户反馈图片太糊，要求：放开到 3 张、vision 分 >0.8 才进日报、"用原图不压缩"。

- **糊的根因不是压缩**：管线全程字节原样（wx extract → KV → TG 当场抓取），实测确认糊图源头是**微信缩略图**——本地库里没在设备上点开过的图只存 96×210 thumb，`wx extract` 只能拿到缓存有的东西（昨天演示那张就是 96×210/5KB，vision 还打了 0.9 分照样入选）。这是 wx CLI/微信缓存的边界，管线无法凭空拿到原图。
- **修法 = 三重收紧**：(1) `analyze_media_candidates` 进 vision 前加 `_is_valid_image_file` 门槛（≥10KB 且 ≥300×300，PIL 检测，pillow 加入依赖）——缩略图不喂 vision 也进不了日报；(2) `min_include_score` 0.65→0.8；(3) `resolve_citations` 默认 `max_images` 1→3，prompt 同步"最多 3 张、确有印证才引用"。`_push_rich_digest` 改多图（每图独立 KV 上传、按 local_path 去重、finally 全量清理），回退路径改为遍历发送全部引用图。
- **昨日数据回测**：6 张旧标准通过的图，新标准下 2 张 96×210 缩略图 + 2 张 0.70 分被挡，剩 2 张清晰大图（531×800、595×1280）——精准命中用户抱怨的糊图。
- **TG 端展示说明**：富消息图片块由 TG 生成多档 PhotoSize 展示（与普通照片消息同机制，平台行为不可绕过）；源图 ≥1280 长边时点开看的最大档观感正常。真"原件文件"只能走 sendDocument 文件卡片，那就不是内嵌图了，不采用。
- 258 passed（重写 vision 过滤 2 例：缩略图/低分排除；cap 改 3 例；AI 优先测试显式 max_images=1）。

### 四修（同日深夜，实跑暴露 wxgf 坏文件 + TG 图偏好 + 端到端全通）
- **实跑第一轮富消息 400 失败**（`RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND`）：LLM 引了 3 张图，其中 `37678.jpg` 文件头是 `wxgf`——**微信私有格式**，`wx extract` 原样导出但套 .jpg 名，PIL/Telegram 均无法解码（连 sendPhoto 都 3 连 400）。它能混进引用池是 `_is_valid_image_file` 的历史宽容逻辑：PIL 打不开→跳过检查放行（pillow 非依赖时代的降级路径）。回退保险按设计工作：文字照常送达 + 2/3 张好图独立发出，日报未丢。
- **修复**：pillow 已是硬依赖，`_is_valid_image_file` 改为 PIL 解析失败→直接判无效（`undecodable image`），wxgf/损坏文件进不了 vision 更进不了引用。
- **TG 图偏好**（用户要求）：`build_citation_block` 排序改为 `(platform=="Telegram", value_score)` 降序——TG 图（≥1280 原图质量）严格优先于微信图进入引用池；引用指令同步加"同等相关时优先 Telegram 来源"。
- **修复后重跑 07-01 全链路真推成功**：`vision analyses included: 2 (citable: 2)`（wxgf+缩略图全被门槛挡掉，恰余两张电丸清晰大图）→ `TG push complete (single rich message, 2 inline image(s))`，单条图文混排，无回退。附带收益：vision 阶段 17min→3min（缩略图不再空耗视觉分析）。
- 259 passed（新增 TG 偏好排序 1 例）。
