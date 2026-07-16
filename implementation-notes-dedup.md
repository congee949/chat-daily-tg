# Implementation Notes — 推送去重分层体系（L1/L1x/L2）

计划：`~/.claude/plans/chatdaily-telegram-deep-star.md`（2026-07-16 获批）。

## Design Decisions

- **L1x 跨 producer 层：度量后 NO-GO，不建机制。** 预写 gate（时间序命中 ≤1/月不建）；实测 6 天窗（索引 07-10→07-16，217 条，健康断言通过）：106 条已送达频道帖、6 条含推链、4 条裸链接，**三个时间桶命中全 0** → 0/月。scp 拉索引、pull 脚本、launchd 移相全部不做。
- **`XMonitorIndex` 保留为休眠能力**：类已实现并测试（TTL 过滤、24h staleness=视为不存在、assumed 条目跳过），但集成层不构造它（`check_duplicate(xmon=None)`）。fming_weekly 入组后复测若翻案，接一根构造线即可启用。
- **bare URL 提取允许 ASCII 括号、尾部按括号配平剥离**：`wiki/Foo_(bar)` 存活、prose 的孤立 `)` 剥掉。全角括号/CJK 标点/汉字终止 URL（治语料里 46 处中文胶连）；代价是罕见的裸 CJK path URL 被截断 → 指纹不同 → 只丢抑制不丢投递（允许的失败方向）。
- **markdown label 计入正文**：`[北京朝阳](url)` 的 label 算实质内容 → 偏向「有评论」判定 → 偏向照发（安全方向）。
- **裸链接阈值 ≤10 个实质码点**（unicodedata 类别 P/S/Z/C 之外），替代设计稿的 <30 字符英文校准；21 字中文策展评语实测判为实质评论。
- **URL 指纹只在同一张表**：`content_seen(fingerprint PK)`，`text:<sha1>` 与 `url:<sha1>` 混存，INSERT OR IGNORE 首发者持有指纹。
- **journal 独立小模块 dedup_journal.py**：L1/L2 共用，write 永不 raise；today_counts() 供日报页脚。
- **`dedup: false` 是整层豁免不是只免抑制**：opt-out 频道既不被抑制也不注册指纹——「首发在豁免频道的内容不会压制后来的重复」是接受的语义（豁免=该频道完全不参与本层），集成测试锁定此行为。
- **register_sent 信任 send_card 的返回**：真实 TelegramSender 返回 list[int]；自定义 sender 若返回 None 会把 None 写进 delivered index（仅影响非常规调用方，记档不防御）。

## Deviations

- 原计划 Phase 2（scp pull + XMonitorIndex 接线 + Minute 移相）依 gate 判决取消——不是范围缩水，是度量先行机制按预期工作。
- implementation notes 用本文件（-dedup 后缀）：仓库根的 implementation-notes.md 属于另一在飞任务（health briefing），不覆盖。

## Open Questions / Blockers

- **L2 校准阻塞在群成员资格**：tg-cli 会话 = Congee 小号（@Congee123, id 8113034240），通知群 -1004424841223 里没有它（`tg info` Could not find chat）。需要用户把该账号拉进通知群，校准与 DeliveredIndex 的 ingest 才有数据面。设计已预期此探测（校准脚本第一步 fail-loud）。
- 私有频道（科技圈在花）不落 tg-cli messages.db（Telethon 直下）→ L1x 度量对它盲测（已在脚本输出加 ⚠ 警告）。对 L2 无影响（L2 吃的是通知群本身）。

## Watch items（校准可跑后核对）

- 单行 📢 卡 normalize_for_embedding 后为空（头行被剥）→ 校准报告的「skipped <24 chars」计数会暴露真实占比；若过高需调整归一化保留正文首行。
- `tg info` 失败时 exit code 仍为 0（"Could not find chat" 在 stdout）——校准脚本已按输出嗅探而非返回码判断，其他脚本复用时注意。
- guess_producer 的 macrumors 模式是占位——enforce 模式前必须用真实落库形态硬化（校准报告 §corpus 提供样本）。

## Tradeoffs

- 度量窗口只有 6 天（索引 TTL 上限 14 天、生产开启于 07-10）——但 blindspot 阶段对 5 个月本地语料的预跑（裸链接∩被监控账号=1 条）与 6 天实测互相印证，NO-GO 结论稳健。
- t.co 不解析（语料 0 例）；article 壳推文 `t:` 不入 x_monitor 索引 → 保守放行。

## 校准日补记（2026-07-16 晚）

- **校准脚本外推假设满窗语料**——实际新号只见 23h 历史（群隐藏历史，回填 0 行），脚本判 STAYS DARK 是错的；人工校正 ≈1 对/天 → GO，更正节已追加进报告文件。教训：**外推前先核 time span**，脚本下版应按真实跨度归一。
- **L2 report 模式当日开启**（config.yaml + .bak-l2report 备份）；judge 构造 bug（LLMClient 签名）由 fail-open 兜住后修复（commit 3933b05），真机复跑零警告。
- **x_monitor 卡片 tg-cli 文本为空**（44/45 空行踩 :00/:30 节拍）→ L2 覆盖面限于文本可见 producer；X 侧话题碰撞归未来 x_monitor register/bundle 方案。
- 复校准窗口：~07-30（DeliveredIndex 积累 2 周后），届时再定阈值、审 journal、升 annotate。

## 归因更正（2026-07-16 深夜，用户指出）

- 「新号只见 23h 历史」的真实原因是**用户对通知群设了每日清除聊天记录**（服务端只留 ~24h），不是我先前写的「群对新成员隐藏历史」——111 条可见 ≈ 一天真实流量的算术证实。校准报告更正节已同步改。
- 三个设计后果：①L2 索引不受影响（本地 sqlite 是持久层，channels 每 2h 同步一次、远早于清除线）；②🔁 标注深链对 >24h 匹配是死链，annotate 模式前置项：带链标注限制在 <24h 匹配或接受死链；③Mac 电池+合盖睡 >24h 的间隙推送会在清除前无人捕获 → 索引永久缺口（欠抑制，安全方向）——已知睡眠盲区的新增后果。

## PR 对抗式审查与修复（2026-07-16 深夜，8 finder → 19 项修复）

**流程级事故（P0，已修）**：并发会话在飞的 run_daily.py 改动（health_card/health_rich 导入、send_rich_message(media=)）被本会话 6f44f4f/3933b05 提交时不慎扫入，依赖文件却未提交——HEAD 曾 4 测试红、健康晨报与富消息在干净检出上是死的（fail-open 掩盖）。修复=收编切片 b92eecd 落齐依赖，HEAD 复绿。教训：**共享工作树上 `git add <file>` 前必须 `git diff --cached` 核对是否扫入他人 hunks**。

正确性修复：私有相册裸链接误杀（媒体帖免抑制——caption 不是内容本体）；DM 回落污染 DeliveredIndex（forum 守卫 + group_internal_id 从 forum_chat_id 派生）；digest 自相似污染 L2 检索（daily_summary 模式硬化，chunk2+ 归因缺口留给校准核对）；页脚把 report/annotate 计入去重 + UTC/CST 日界错位（today_counts 只数 skip、按北京日）；L2 journal 缺被抑制卡自身 ids → assess(ref=)；exclude_patterns 帖零留痕 → journal；度量脚本用原文而非 strip 后文本分类（已对齐；NO-GO 结论不受影响——命中通道 yihong 无 strip_patterns）；xmon detail 形状统一；topic.enabled 而 judge alias/embedding 缺失 → config 加载期 fail-loud（原先是静默逐轮空转）。

复用/效率：judge 构造改用现成 `_llm_from_block`（此前手搓签名正是 20:52 生产 bug 的根因）；GeminiEmbedder.from_config 统一三处构造（校准=生产同一嵌入）；cosine 委托 evidence_index；零新卡轮免群同步/嵌入（惰性 ingest）；recent() SQL 过滤 + ts 索引 + 每轮解码缓存；L2 双路径 40 行复制抽成 _l2_check/_l2_register；私有 prepare 不再给 excluded/媒体帖花嵌入配额；文本路捕获 send_card ids 复活 register_sent。

文档矛盾五处同步修：CLAUDE.md write-after-send 增加终态抑制例外条款（journal+--resend 为前提）、marker 清单补 .health-card-sent、KV 中转→多部分直传（CLAUDE/README/ARCHITECTURE 三处）、run_daily docstring schedule.timezone 事实修正、spark 旧 config 键 as-built 化。

**记档不修**：topic_dedup 的 JSON 解析器未并入全仓统一（4 模块解析器合并是独立重构，fail-open 已兜底）；exclude_producers 双份默认值改测试锁定等值（避免 config↔topic_dedup 导入环）；digest chunk2+ 归因（结构修复=digest 推送路径直接 register_sent，等校准数据）；`dedup: false`=整层豁免（有意语义）；journal 无 version 字段；health prepend/strip 回环（并发会话活代码）。
