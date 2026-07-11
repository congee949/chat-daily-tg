# Implementation Notes — 成长内容挖掘 (growth mining)

计划文件：`~/.claude/plans/22-20-23-00-mighty-shannon.md`（已批准）。只记录与 spec 的实质偏离和现场决策。

## Design Decisions

- **getUpdates 每日轮询而非周轮询**：Bot API 更新只保留 ~24h，周六才 poll 会丢一周反馈。日常任务尾部 `poll_dm_feedback()` 落 `feedback-inbox.jsonl`（offset 全量 ack，含被忽略的群消息），周六消费。是对用户原表述「周报时收反馈」的有意偏离。
- **rubric 文件助手放 growth_store**：`ensure_rubric/rubric_version_of/DEFAULT_RUBRIC` 被 cards（judge 要读）和 weekly（merge 要写）两侧共用，放 store 避免 cards↔weekly 交叉依赖；store 已有 slice 文件 I/O 先例。
- **每日编排放 run_daily.py 而非 growth_cards**：`run_growth_daily` 流程需要 `resolve_tg_target`（定义在 run_daily.py）；若放 cards 会产生 run_daily↔cards 循环导入或注入式参数。repo 先例 `run_bilibili()` 同样把编排放入口文件。cards 模块只保留纯构卡/评审函数。
- **`--dm-test` 不 mark_sent、不占 quota、不 poll**：它是上线前排版验证，消费段落会导致真发时换段。
- **挖掘部分失败（GrowthMiningError）不阻塞当日推送**：好 chunk 的段落已入队，照常从队列发卡；当天不标记 mined，次日 catch-up 自动重挖；notify_failure 已告警，入口仍返 0。
- **回填对已挖天不做前置检查**：`mine_day` 自身天级幂等（快速返回），回填循环无条件调用 + 固定 sleep 1s，换 75s 最坏重跑开销省一个 chat_id 形态换算分支。
- **模型走 `--model llm`（deepseek-v4-pro 别名）**：live config 的 `models.summary` 是 gemini-3.5-flash-low；挖掘/评审质量敏感，wrapper 固定传 `--model llm`。日常 ~3 次调用/天。
- **溯源双层**：卡片尾注 `#msg<start>–<end>` + URL 按钮 `t.me/c/<id>/<start>`；本地切片 `~/chat-daily/growth/segments/YYYY/MM/DD-<start>.md` 含 span 全部消息（含短消息/emoji，存档不滤噪）。`tme_link` 同时接受 DB 正数形态与 `-100` 标记形态（首版实现漏了剥前缀，Wave 0 自查修正）。

## Deviations

- **cfg.growth.weekly.weekday 仅作文档**：周六调度由 plist `Weekday=6` 强制，入口函数不做星期检查（手动随时可跑）。
- **切片只为 pending 段落写**：rejected 段落进 DB（挡重叠重挖）但不落切片文件。
- **weekly.merge_rubric 返回三元组 (text, version, changed)**：计划文本写的二元组，加 `changed` 供周报标注「本周已按你的反馈更新」。

## Tradeoffs

- **重叠去重用 msg_id 宽度近似消息数**：ids 非稠密，宽度≈条数是近似；但两侧同尺度比较，阈值 0.5 的判定方向一致。换真实条数需回查 DB，不值得。
- **A/B 风格对 = 确定性模板 vs LLM 叙事**：省调用（每天 ≤3 次）且对比的是真正不同的消费体验；判负时回落 A（零捏造风险侧）。

## 对抗评审裁决（Wave 3 跨厂商面板，2026-07-11）

已修（本次）：
- fable 自查：weekly 里 consume_inbox 改名后 merge_rubric 失败 → 反馈写回 inbox 再抛（P1）；getUpdates 非空批零推进的死循环守卫（P2）。
- Grok：金句长度窗口 6–160 字（挡 "是" 碎片与断章面，同时封死卡片多 chunk）；金句展示文本改为**DB 逐字反查片段**（不再展示 LLM 排版版）；零有效金句的 pending 段落降级 rejected（无可信锚点即不推送）；merge_rubric 输出 <40 字视为失败保持现版；judge reason 入库截断 200 字。
- Kimi：backfill 终点改 today-2，昨天独属 daily 任务，并发撞日窗口整体消除；ab_stats 改为每段取最新判决，重评审不再虚增胜率。

明确接受不修（记录理由）：
- send 成功→mark_sent 前崩溃的重发窗口：at-least-once 是计划明选（宁重发不丢卡），窗口毫秒级，私有 topic 重复可见可删。
- 周六 09:30/09:45 两次 poll 理论并发 409：非长轮询，尾部 poll 秒级；即便撞上，反馈仍在下次 poll 进 inbox，最坏延迟一周不丢失。
- B 卡叙事可含未经逐字校验的事实陈述：生成式导语的固有属性，靠 prompt 禁令 + rubric + judge + 回落 A 兜底，用户选定的 A/B 设计本身。
- rubric 全文由 LLM 重写落盘：rubric 治理本来就走"用户 DM 反馈→LLM 合并"，版本化 + 周报披露版本变化即审计线。
- messages.db 读与 tg sync 并发的 SQLITE_BUSY：预存在风险类（main/channels 同样暴露），失败可见且 catch-up 自愈，不在本次改共享代码。
- 毒丸日（某 chunk 永远解析失败）：每日三次告警可见，人工用 --growth-mine-day 介入。

## Wave 4 真机验证记录（2026-07-11）

- 评审面板收尾：opencode glm-5.2 挂死（29 分钟零落盘、单 TCP 空转）→ kill；按用户指示跳过第三路，两路评审 + fable 自查已覆盖。
- 挖 7/10：2 段入队——回本段 `1782517–1782652`（score 9.0，与锚点 1782515–1782652 重叠 99%，LLM 从 J1mmy「住回本了」起段更完整）+ 减脂自律段（7.0）。金句三条全逐字（含 1782520 带空格原文）。
- dm-test → 真发 thread 497 均成功；judge 判 A 9.0 : B 3.0（判词命中默认 rubric 的拒鸡汤条款）；重跑正确触发 quota 守卫。
- getWebhookInfo：无 webhook、0 积压，getUpdates 独占确认。
- launchd 四任务注册成功（growth 09:30/15:30/21:30、growth-weekly 周六 09:45）。
- config.yaml 已加 growth 块；调度 wrapper 固定 `--model llm`。
- 回填 4/27→7/09 后台进行中。

## 上线后用户反馈迭代（2026-07-11 当天）

- **源群消息一天一清**（用户补充的关键事实）→ t.me 深链对任何账号 24h 内必成死链：删掉卡片「↗ 跳转原文」按钮与切片头部链接行，`tme_link` 成死代码一并移除。本地切片升级为原文唯一长期载体——回溯地正是最初需求「确保能在本地找到原始对话」。
- 尾注两轮精简：`#msg…` 先改纯文本（避免渲染成话题标签），再按用户选择收敛为**日期+时段**极简版（`📍 2026-07-10 · 22:22–23:07`）。裁决依据：日期承重（存货卡内容日期≠发送日期）、时段区分同日多段；群名单一来源纯重复、msg 区间在切片文件名与 DB 双冗余，均不上卡。
- 账号拓扑现状记录：电丸成员是 Congee(8113034240，tg-cli session)，forum 阅读账号是 congee949，两账号互不在对方群——即便消息不清，深链在此拓扑下也无人可点。

## 富文本升级（2026-07-11，用户选「全套采用」）

- **要点关键词加粗**：miner prompt 允许 LLM 在 point 里用 `**…**` 标 0-2 处重点；`_clean_points` 长度按去标记纯文本算（超限丢标记截断）；渲染层先 `escape_html` 再成对转 `<b>`、落单剥除。纯格式层，不触碰金句逐字信任边界；存量无标记段落按素文本渲染。
- **原话 → 原生引用块**：`<blockquote>原文\n— 发言人</blockquote>`，去空白 >120 字自动 `expandable` 折叠（顺带彻底锁死卡片单 chunk）。
- **落款斜体化 + 去分隔线**：`<i>📍 日期 · 时段</i>`，引用块自带视觉分隔。
- **INDEX.md 快查索引**（同日用户需求）：`growth/segments/INDEX.md` 每次挖到带切片段落后由 DB 全量重建（幂等），一行=日期 时段·主题·msg 区间·切片链接；卡片端零 msg 噪音、查询端一步直达。

## Open Questions

- 电丸群若未来改名/迁移，`cfg.growth.source` 需手动更新（chat_id 变化会让 mined_days/segments 的 key 断代，历史仍可查）。
- 跨午夜对话被日界切断（与日报同行为）；如实际命中率高再考虑 ±2h 重叠窗。
