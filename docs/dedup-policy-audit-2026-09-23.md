# 重复消息过滤原则审计（2026-09-23）

范围：以 2026-09-22 Claude Opus 5.5 发布当晚的实际推送为样本，审计两条生产链路（BWG `x_monitor` 与 Mac `chat-daily-tg`）现有的去重规则，回答三个问题：

1. 官方号（ClaudeDevs、claudeai）与员工账号围绕同一公告的多条推送如何过滤；
2. vista8、dotey 这类博主对原推的翻译、摘译、信息图是否保留；
3. 科技圈在花频道晚于 X 到达的同一事件如何针对性过滤。

第 1–9 节保存 2026-09-22/23 的审计快照与原始方案；第 10 节记录本次已落地的实现。运行配置、远端 cron/launchd 和 Telegram 内容仍不由本文直接修改。所有事实来自只读检查：BWG `state/x_monitor_sent_content_ledger.jsonl`、`twitter_seen/.event_ledger.sqlite3`、`twitter_seen/.semantic_decisions.jsonl`、`/var/log/x_monitor.log`；Mac `~/chat-daily/state/dedup_journal.jsonl`、`delivered_index.db`、`logs/channels-2026-09-23.log`；tg-cli `messages.db` 中通知群（-1004424841223）的同步副本。时间统一用 UTC，括号内为北京时间。

## 1. 结论摘要

| 问题 | 现状 | 判定 |
|---|---|---|
| 官方号 + 员工同源转发 | 五条引用同一 claudeai 原推的卡片（ClaudeDevs、trq212、bcherny、vista8 经 addyosmani、dotey 经 addyosmani）全部投递；事件级去重只在 observe 模式，且只覆盖带 push_policy 的官方号 | 规则缺口，需新增“同锚点引用折叠” |
| 博主翻译 / 信息图 | 已有 `quote_translation` 机制，但只改展示（把引用者换成原作者署名），不阻止重复投递；长文摘译因“相对被引推文有新增信息”一律 keep | 保留有增量的长文，折叠纯翻译；信息图需单独判定 |
| 在花频道晚于 X | 在花 00:30 发帖与 X 官方推文同分钟，但 Mac 频道任务 22:00→06:00 无排期，06:09 才投递；L2 话题层此时看不到 x_monitor 图片卡片（tg-cli 不存 caption），且处于 report 模式 | 三个独立缺口叠加，需分别处理 |

一句话：现有各层对“同一条推文 / 逐字转发 / 同一裸链接”已经足够，但对“同一事件的不同壳”几乎没有实际抑制。所有相关闸门都被设计成 fail-open（宁可重复，不可误杀），今天看到的重复是这条原则的直接代价，而不是 bug。

## 2. 现有过滤链路盘点

### 2.1 BWG `x_monitor`（X 推文 → 通知群 thread 19 / 1145 / 1146）

cron `*/30`，每轮按账号顺序处理。判定顺序：

1. `seen`：按 `(account, tweet_id)` 幂等。
2. 原创硬门 `_originality_gate`：纯 RT（`retweeted_status`、`RT @` 前缀）和空评论引用直接 `filtered_terminal`。官方号策略下纯 RT 不推送；curator 账号允许 repost 作为选品触发，穿透到 anchor。
3. 账号策略（仅 5 个官方/员工号有 `push_policy`）：`claude_dev_original`、`claude_entitlement_original`、`openai_dev_original`、`openai_major_original`、`codex_quota_original`。事件类型不在允许集合内的推文按 `policy:*` 过滤。当晚实际过滤：ClaudeDevs 的 safeguards 推文（`no_developer_event`）、claudeai 的 “Together, these advances…”（`no_entitlement_change`）、thsottiaux 的预热推文（`no_completed_quota_event`）。
4. 跨账号去重 `cross_account_dedup`：以 semantic bundle 的 `bundle_key`（`t:<anchor_id>` / `a:<article_id>`）查 `.pushed_index.json`。只折叠纯转发路径；**Quote 永不 alias 到被引推文**（`2026-08-13` spec §3.5）。当晚命中 1 次：bcherny 纯 RT trq212 → `anchor_already_delivered`。
5. 事件账本 `event_identity` + `.event_ledger.sqlite3`：`event_key = 事件族 + 首个模型名/URL 锚点`，72 小时窗口，比较 facts 子集。**生产模式 `observe`**，Go/No-Go 报告 `reviewed=0 / min_reviewed=20`，不满足 enforce 条件。只有 `event_type` 非空（即官方号策略分类过）的推文 `confidence=high`，curator 推文 `event_type=''` 永远 low，不参与事件比较。当晚唯一一条 `would_suppress` 观察：claudeai 18:55 “Claude Opus 5.5 is available today. What will you explore?” 对 16:31 的原推；因 observe 模式仍投递。
6. `quote_translation`（仅 curator、跨语言引用、resolution complete）：LLM 判断引用者文字是否仅是被引原文的翻译/忠实摘述。`translation_only=true` 且 `confidence≥0.95` → `action=source_only`，**只改渲染**：卡片署名换成原作者、引用者进入 `repost_path`（显示“经 @dotey 转发发现”）。不查询 pushed index，不阻止投递。
7. `_review_information_quality`：LLM 判低信息量（日常打卡、闲聊）→ `low_information` 过滤。近一周 `filtered_terminal` 中该原因约 15 条。
8. 投递后 `_record_pushed` 写 pushed index，`deliveries` 表写 `confirmed`。

### 2.2 Mac `chat-daily-tg`（频道转发 → thread 41 / 1146）

launchd `channels` 每日 9 次触发：06、09、10、12、14、16、18、20、22 点（`schedule.yaml`），每次 0–15 分钟 jitter。**22:00 到次日 06:00 没有排期。**

判定顺序（`raw_channels.push_raw_channel_cards`，私有频道走 `private_media` 同位闸门）：

1. `SeenStore`：`(chat_id, msg_id)` 幂等 + 增量高水位。
2. `exclude_patterns` / `strip_patterns`：按频道正则整帖排除或去掉推广行。
3. 同轮跨频道 premerge：按源时间排序后，同一 text/title/URL 指纹只保留先到的一张。
4. L1 `content_seen.check_duplicate`（14 天窗，`content_seen.db`）：
   - 正文指纹相同 → skip（`text`）；
   - 首行标题指纹相同 → skip（`title`）；标题 bigram Jaccard ≥ 0.88 → skip（`title_fuzzy`）；
   - 裸链接帖（去链接后 ≤10 个实质字符）URL 相同 → skip（`url`）；
   - 非裸链接帖 URL 相同且 `url_authority_skip: true`（当前生产已开）→ 先到者胜，后到 skip（`url_first`）；
   - `XMonitorIndex`：读取 x_monitor pushed index 的本地副本，命中 `t:`/`a:` 键 → skip（`xmon`）。**当前生产未构造该副本（2026-07-16 度量 NO-GO 后休眠）**，实际不生效。
   - 纯媒体帖按文件 sha1 去重（`media`）。
5. L2 `topic_dedup.TopicDedupGate`：对 `delivered_index.db` 中 14 天内已投递卡片做向量检索（Qwen embedding，on-demand），候选 ≥ `candidate_min_sim` 后由 Jev 主裁判 / SameEventJudge 备裁判判定 `same_event` 与 `new_info`。**生产 `mode: report`、`enforce_enabled: false`**：只写 `dedup_journal`，不 skip、不标注。`embedding_coverage` 昨日 0.990 < 0.995 门槛，即便配置 annotate 也会降级为 report。
6. 发送后 write-after-send：`seen`、`content_seen` 指纹、`delivered_index.register_sent`、`sent_content_ledger`。

`delivered_index` 的另一条进货路径是 `ingest_new`：同步通知群到 tg-cli `messages.db`，再逐条入库。**tg-cli 对图片消息不保存 caption（`content` 为空），`ingest_new` 对空内容 `continue`。** 2026-09-22 15:00 UTC 至今通知群 109 条消息中 61 条 content 为空；同期 x_monitor 实际推送约 50 张卡，进入 delivered_index 的只有 6 张（`sendMessage` 纯文本卡）。x_monitor 默认 `sendRichMessage`（带图），所以 L2 基本看不到 X 卡片。

### 2.3 生产配置快照（只读）

Mac `~/chat-daily/config.yaml`：

```yaml
dedup:
  content: {enabled: true, window_days: 14}
  topic:
    enabled: true
    judge_provider: jev
    mode: report
    enforce_enabled: false
    reranker_enabled: false
    min_embedding_coverage: 0.995
  authority:
    url_authority_skip: true
```

BWG `/root/x_monitor/config.json`（去密钥）：`cross_account_dedup: true`、`semantic_bundle_enabled: true`、`semantic_bundle_shadow: false`、事件账本 `requested=observe`。`twitter_accounts.json` 中 trq212、bcherny 与 dotey、vista8 同为无 policy 的 curator，虽然前两者是 Anthropic 员工。

## 3. 案例复盘：Opus 5.5 发布

claudeai 原推 `2102435511222890900` 发布于 16:31:01（00:31）。以下是通知群中围绕该事件的全部卡片，按投递时间排列。

| 投递(UTC) | thread | 来源 | 与原推关系 | 通过原因 |
|---|---|---|---|---|
| 17:00:03 | 19 | ClaudeDevs `…800836489554` | Quote 原推 + Claude Code 额度细节 | `quota_policy` 放行；Quote 不 alias |
| 17:00:05 | 19 | ClaudeDevs `…808952467507` | 独立原创 “available now in Claude Code” | `dev_release` 放行 |
| 17:00:08 | 19 | claudeai `…511222890900` | 原推本体 | `model_launch` 放行 |
| 17:00:11 | 19 | claudeai `…538120691886` | 自回复：五小时额度 + 重置卡 | `quota_policy` 放行 |
| 17:00:25 | 19 | dotey → addyosmani `…173804818494` | dotey 引用 addyosmani，文字为纯翻译 → `source_only` 改署名 addyosmani | 展示层改写，投递照常；addyosmani 本身是对原推的转述 |
| 17:00:27 | 19 | dotey `…440266875449386` | 长文中文摘译（价格、案例、HAProxy） | `quote_translation=keep`：相对被引推文有新增信息 |
| 17:00:36 | 19 | trq212 `…437686967738431` | Quote 原推 + 一句员工视角 | 无 policy，curator 默认放行 |
| 17:00:45 | 19 | bcherny `…439069053747549` | Quote 原推 + HAProxy 数据 | 同上；其另一条纯 RT trq212 被 `anchor_already_delivered` 折叠 |
| 17:30:18 | 19 | vista8 → addyosmani `…445889503760482` | 引用 addyosmani，补三个榜单名 | `keep`：有新增 |
| 17:30:22 | 19 | dotey `…443590748197249` | “Claude Code 也有重置卡了” | 原创短评 |
| 18:00:05 | 19 | vista8 `…452507993919719` | 长文中文摘译（榜单分数、案例） | 原创长文 |
| 19:00:04 | 19 | claudeai `…471892099866883` | “available today. What will you explore?” | `model_access` 放行；事件账本 `would_suppress` 但 observe 不执行 |
| 22:09:42 | 41 | 科技圈在花（频道 00:30 发帖，私有媒体路径） | 中文新闻稿 | L1 无指纹命中；L2 report 且索引里没有 X 图片卡 |
| 22:30:37 | 41 | MacRumors 日报条目 | 中文一句话 | macrumors 无跨 producer 去重 |
| 00:15:30 | 486 | bilibili 神烦老狗 | 视频实测 | bilibili 仅按 bvid 去重，且 L2 排除 |
| 04:02:55 | 41 | Reorx’s Forge | 一句使用感受 | 有个人观点，属于合理增量 |

thread 19 在 17:00–19:00 收到 12 张与该事件相关的卡片，其中至少 6 张在“告诉用户发生了什么”这一层没有增量：ClaudeDevs Quote、trq212 Quote、bcherny Quote、dotey→addyosmani、vista8→addyosmani、claudeai 19:00。三张长文摘译（dotey、vista8 各一，addyosmani 一）互为重复。

## 4. 问题一：官方号与员工同源转发

### 4.1 为什么现在拦不住

- 跨账号去重的身份是 anchor tweet ID。Quote 按 spec 是新 bundle（“B1/B2 分别 Quote C 不能因共享 C 互相去重”）。这条规则对 curator 是对的（评论是增量），但对官方号和员工引用自家公告，评论通常是同一公告的另一角度重复。
- 事件账本本可折叠 claudeai 19:00 那条（已观察到 `would_suppress`），但 enforce 需要 20 条人工复核样本，目前 0 条。
- trq212、bcherny 在 `twitter_accounts.json` 中没有 `push_policy`，走 curator 路径，事件账本对其 `confidence=low`。

### 4.2 建议规则

规则 A：**同锚点引用折叠（官方 / 员工圈内）**。定义一个账号组 `anthropic_official_circle = {claudeai, ClaudeDevs, trq212, bcherny}`（OpenAI 同理：`{OpenAI, OpenAIDevs, thsottiaux}`）。组内账号 Quote 组内账号已送达的 tweet 时，不再作为独立卡片投递，而是：

- 若被引 anchor 已在同一 target thread 送达（查 pushed index / deliveries 表），把 Quote 评论作为“补充”附在原卡下方（Telegram reply 到原卡 message_id），或合并进一张“事件卡”；
- 若评论没有新增结构化 facts（事件账本 `candidate_facts <= prior_facts`），直接 `duplicate_terminal`，写 semantic journal，不发送。

这是对 spec §3.5 的有界例外：例外只对显式配置的账号组生效，curator 引用官方推文仍按原规则处理。

规则 B：**事件账本推进到 enforce 的最小路径**。Go/No-Go 门槛是 20 条复核样本、误判率 ≤2%。`event_observations` 已有观察记录，需要人工标注脚本（`event_ledger_review.py` 已在仓库）跑一轮。enforce 后 claudeai 19:00 那类“同事件无新事实”会被折叠（当晚的观察记录证明 `model_launch` 与 `model_access` 已归入同一 event family，锚点 `opus 5.5` 也已对上，只差执行模式）。

规则 C：**员工账号补 policy**。trq212、bcherny 加 `push_policy`（如 `claude_staff_original`），允许事件集合 = `dev_release`、`model_api`、`quota_*`，纯感想（“It's been my daily driver”）由 `_review_information_quality` 处理。这样它们的推文进入事件账本 high confidence 路径。代价：员工的非公告类内容（工程细节、个人观点）会被过滤；如果用户想保留这些，改为规则 A 就够。

规则 D：**thread 合并**。官方号同一 `conversation_id` 的自回复（claudeai 16:31 + 16:31 自回复；ClaudeDevs 两条）按 2026-07-18 设计 §5.1 的 90 秒 idle window 合并为一张卡。该设计当时排在“落地顺序”最后一项，目前未实现。

## 5. 问题二：博主翻译与信息图

### 5.1 现状

`quote_translation` 已能识别纯翻译（dotey→addyosmani 当晚被判 `translation_only, confidence≥0.95`），但处理方式是“展示原作者”而不是“抑制”。理由记录在代码注释：“Pure translations already follow the user's source-only presentation policy”——即用户之前的决策是看原文不看译文，而不是不看。

长文摘译（dotey 16:49、vista8 17:38）的判定对象是**被引推文**，而它们实际摘译的是 Anthropic 博客。相对一条 280 字推文，它们当然“有新增信息”，所以 keep。两篇之间互为重复，但没有任何层比较它们。

信息图（vista8 常见形态：一张图 + 几行字）目前没有针对性规则。`_prepare_quote_translation` 里对“引用者额外图片”有一段 LLM 图像判定（`redundant`），只在文字已判纯翻译时触发。

### 5.2 是否保留：分三档

| 形态 | 建议 | 理由 |
|---|---|---|
| 纯翻译 / 忠实摘译，被引原推**已送达**同一 thread | 不投递独立卡；可 reply 到原卡附一行“中文摘译 by @dotey”并带链接 | 用户已看到原文；译文价值是可读性，reply 形式保留这个价值 |
| 纯翻译，被引原推**未送达**（被引账号不在监控列表） | 投递，署名原作者（现有 `source_only` 行为） | 这是用户获得该原文的唯一途径 |
| 摘译长文（有价格表、案例、榜单数据） | 投递，但同事件只保留第一篇；后到者以 L2 `new_info=minor` 标注 🔁 并深链首篇 | 长文有整理价值；两篇长文互为重复 |
| 信息图 | 投递；同事件多张信息图只保留第一张 | 图无法与文字比较，L2 embedding 也看不到图。按“同事件 + 有图 + 图片 sha1 不同”视作 minor 增量处理 |
| 博主原创评论（“Opus 5.5 写作非常好”） | 投递 | 这是订阅 curator 的目的 |

### 5.3 落点

- 翻译折叠的判定信号已存在（`_quote_translation.action == source_only` + `source_id`）。缺的一步是：拿 `source_id` 查 pushed index / deliveries 是否已送达同 thread。已送达 → 不建新卡；改 reply。这是一个小改动，且 fail-open（查不到就按现状投递）。
- 长文互斥依赖 L2。x_monitor 侧没有 L2；Mac L2 看不到图片卡。见第 6 节的数据面修复。

## 6. 问题三：在花频道晚于 X

### 6.1 根因拆解

| 缺口 | 事实 | 影响 |
|---|---|---|
| 排期空窗 | 在花 00:30 发帖，Mac 频道任务上一轮 22:00，下一轮 06:00 → 06:09 投递 | 5 小时 39 分延迟。此类“晚到”不是频道慢，是 Mac 没跑 |
| L2 数据面盲区 | tg-cli 不存图片 caption，`ingest_new` 跳过空 content；x_monitor 默认带图卡 | 06:09 时 delivered_index 里没有 claudeai/ClaudeDevs 卡片，L2 无候选，journal 无记录 |
| L2 只 report | `mode: report`、coverage 0.990 < 0.995 | 即便有候选也不 skip |
| L1x 休眠 | `XMonitorIndex` 未构造 | 在花卡是新闻稿，无推文链接，L1x 本来也命中不了 |

### 6.2 针对性方案

方案 1：**在花走 x_monitor 已送达索引反查**。在花新闻稿与 X 卡没有共享键，只能语义匹配。可行路径是 2026-07-16 spec §4 的 Phase 4 镜像：把 x_monitor 的 `sent_content_ledger.jsonl`（含完整 caption 正文，BWG 已产出，Mac 也有同 schema 的 `sent_content_ledger.py`）拉到 Mac，作为 `delivered_index` 的第二进货源，替代 tg-cli 空 caption。这一步不依赖 Telegram 读回，直接消除 6.1 第二行的盲区。拉取失败按 `XMonitorIndex` 的现有语义：副本 >24h 视为不存在，只少抑制不多抑制。

方案 2：**L2 对 `chatdaily_raw` 频道卡启用 annotate**。在花卡片匹配到 X 官方卡后，`new_info=none/minor` → 标注 🔁 + 深链，不 skip。这符合 ratchet（report ≥1 周 → annotate → enforce）。前提是 coverage 回到 ≥0.995 或校准回执完成；`jev` 主裁判 22 日已开始产出 `judge-none` 判定，说明链路可用。

方案 3：**排期加 00:00 / 02:00 轮次**，或在 Mac 侧接收 x_monitor 的“重大事件”信号后触发一次频道补拉。前者简单（`schedule.yaml` 加两项，`python scripts/schedule.py apply`），但会把凌晨的所有频道内容也提前推给用户；后者要跨机器，复杂度高。若用户不在意夜间推送，方案 3 前者是最直接的“不晚到”办法；若在意，晚到本身可以接受，只需把晚到的同事件卡折叠（方案 1+2）。

方案 4：**在花卡的标题级去重扩到跨 producer**。在花新闻稿首行是加粗标题（“Anthropic 发布 Claude Opus 5.5，成本降 40%”），MacRumors 条目也有标题（“Anthropic发布Claude Opus 5.5模型：性能看齐…”）。现有 `title_fuzzy`（bigram Jaccard ≥ 0.88）只在 `content_seen.db` 内比较频道卡。若把 MacRumors 条目也注册 title 指纹，在花 vs MacRumors 这一对可以在 L1 层折叠，不需要 embedding。当晚两者 Jaccard 估计低于 0.88（措辞差异大），所以这条只能处理近似标题，不能替代方案 1+2。

## 7. 建议实施顺序

按“改动小、fail-open、可回滚”排序：

1. x_monitor：`source_only` 翻译在被引原推已送达同 thread 时改为 reply 而非新卡（第 5.3 节）。改 1 个函数 + 查询 deliveries；不改身份模型。
2. x_monitor：为 `anthropic_official_circle` / `openai_official_circle` 增加同锚点引用折叠（规则 A）。显式配置账号组；不配置则零行为变化。
3. x_monitor：跑 `event_ledger_review.py` 标注 20 条以上观察，让事件账本满足 enforce 门槛（规则 B）。这一步只需人工复核，不改代码。
4. Mac：`delivered_index` 增加 x_monitor sent-content ledger 进货源（方案 1）。这是问题二长文互斥和问题三的共同前提。
5. Mac：coverage 达标后 L2 切 annotate（方案 2）。观察一周 journal 再决定是否对 `chatdaily_raw` enforce。
6. 可选：`schedule.yaml` 加夜间轮次（方案 3），由用户决定是否接受夜间推送。

## 8. 不建议改的部分

- “宁可重复，不可误杀”与 write-after-send：所有新增抑制路径必须 journal-before-seen，保留 `--resend` 逃生口。上述方案全部遵守。
- Quote 不 alias 到被引推文的通用规则：只对显式账号组做例外，不全局放开；否则 curator 的实质评论会被误折叠。
- `min_include_score=0.8` / `fallback_min_score=0.65` 等 vision 阈值、B站 bvid 去重：与本次问题无关。
- 不要用 defer-one-cycle 处理“同事件晚到”：高水位会静默吞帖（2026-07-16 已列为禁用手段）。

## 9. 复现命令（只读）

```bash
# BWG：当晚推送账本与事件账本
ssh bwg 'cd /root/x_monitor && python3 - <<PY
import json
from datetime import datetime,timezone
for l in open("state/x_monitor_sent_content_ledger.jsonl"):
    r=json.loads(l); t=datetime.fromisoformat(r["sent_at"]).astimezone(timezone.utc)
    if "2026-09-22T16:00"<=t.isoformat()<="2026-09-22T19:30": print(t.isoformat()[:16], r["thread_id"], r["source_ref"])
PY'
ssh bwg 'sqlite3 /root/x_monitor/twitter_seen/.event_ledger.sqlite3 \
  "select observed_at,candidate_username,decision from event_observations where observed_at>=\"2026-09-22T15\""'

# Mac：delivered_index 的 x_monitor 覆盖率
sqlite3 ~/chat-daily/state/delivered_index.db \
  "select producer, sum(norm_text=''), count(*) from delivered where ts>='2026-09-22T15:00' group by producer;"
sqlite3 "$HOME/Library/Application Support/tg-cli/messages.db" \
  "select sum(coalesce(content,'')=''), count(*) from messages where chat_id=4424841223 and timestamp>='2026-09-22T15:00';"

# Mac：06:00 轮次 L2 是否产生候选
rg -n "topic|L2" ~/chat-daily/logs/channels-2026-09-23.log | head
tail -n 5 ~/chat-daily/state/dedup_journal.jsonl
```

相关设计文档：[2026-07-16 raw channel 内容级去重](spark/2026-07-16-raw-channel-content-dedup-design.md)、[2026-07-16 跨 producer 与话题级去重](spark/2026-07-16-cross-producer-and-topic-dedup-design.md)、[2026-07-18 官方 X 推送策略](spark/2026-07-18-official-x-push-policy-design.md)、[2026-08-13 X repost semantic bundle](spark/2026-08-13-x-repost-semantic-bundle-spec.md)、[Jev 话题去重](jev-dedup.md)。


## 10. 实现与验证记录

有效规则、镜像同步、向量回填和人工复核命令见 [去重运行指南](dedup-policy.md)。源码改动、Mac 夜间排期安装结果、BWG 补丁交付与测试数据见 [2026-09-23 实现记录](notes/dedup-policy-implementation-2026-09-23.md)。第 1–9 节保留实施前的现场证据。
