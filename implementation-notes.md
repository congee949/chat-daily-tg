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
