# Spec: 跨 producer 去重（L1x，度量后 NO-GO）+ 话题级去重（L2）+ blindspot 归档

日期：2026-07-16
状态：L1x **度量后不建**（gate 判决）；L2 模块已实现（topic_dedup.py，54 测试），校准阻塞在群成员资格；Phase 4（bwg 反向闸）依校准数据排期
来源：用户提问「相似/相同内容被多次推送，要不要设限、怎么过滤、把 x_monitor 合并考虑、做一次 blindspot pass」。承接 7-10 spec §7 挂账「跨 producer 去重」与 7-11 复盘「话题级归 digest 独立立项」。

## 0. 三层模型（7-11 确立，本次沿用）

| 层 | 含义 | 状态 |
|---|---|---|
| L1 ID/内容级 | 同一条推 / 逐字转发 / 同链裸转 | x_monitor 内部已解决；ChatDaily 频道内 = 同日 content_seen 设计（已实现） |
| L1x 跨 producer 精确级 | 频道裸链接 ∩ x_monitor 已推 | **本次度量后 NO-GO，不建** |
| L2 话题级 | 同一件事不同视角/不同组织 | **本次设计并实现模块**（用户拍板纳入） |
| L3 跨系统 | 个人 X 关注流/RSS | 不可见，仍出界 |

## 1. 用户拍板（2026-07-16）

1. L1x 度量定生死（gate 预写：时间序命中 ≤1/月不建）
2. L1 命中动作维持原设计：指纹命中跳过；URL 命中且裸链接跳过；实质评论照发
3. 送达语义：**全群算一个送达面**（任何 thread 送达即算，无线程条件）
4. L2 纳入本次直接设计（兴趣门控/推送量治理仍出界）
5. L2 judge = VibeKey `gpt-5.6-terra`（luna 备选；/v1/models 实测确认存在）
6. macrumors-after-科技圈 方向也要覆盖 → Phase 4 bwg 侧闸门

## 2. L1x 度量结果（同日实测，NO-GO）

工具：`scripts/measure_cross_producer_dup.py`（与运行时共用 `content_seen.canonical_urls`/`tweet_keys_from_urls` 同一实现）。

- 索引健康断言通过：217 条（t:204/a:13），07-10→07-16，~31 条/天，最新 1.4h 前 → 生产 `cross_account_dedup` 确认开启，排除「无数据≠无重叠」陷阱
- 6 天窗、106 条已送达频道帖、6 条含推链（全部 yihong0618）、4 条裸链接
- **三个时间桶（clean-suppressible / same-window / xmon-later）命中全 0 → 0/月，gate 判 NO-GO**
- 与 blindspot 阶段 5 个月语料预跑（裸链接∩被监控账号=1 条）互相印证
- 已知盲区：科技圈在花走 Telethon 私有路径不落 tg-cli messages.db（送达至 42605、库停在 42312/07-02），脚本已输出 ⚠ 警告；对结论无实质影响
- **复测触发条件**：fming_weekly 入组后重跑一次；翻案则启用已休眠的 `XMonitorIndex`（content_seen.py 内已实现+测试：逐条 TTL、副本 >24h 视为不存在、assumed 条目不作抑制依据）

否决的机制（不再建）：scp 拉索引脚本、run_channels_guarded 挂载、channels launchd Minute 0→15 移相、x_monitor `_record_pushed` 的 assumed:true 标记（无消费方）。

## 3. L2 话题级设计（已实现 `src/chat_daily_tg/topic_dedup.py`）

**底座洞察：交付面本身是共享底座**——所有 producer 都落同一个通知群（-1004424841223），tg-cli 同步它即可反推「已送达内容」，零跨机管道。约束（已验证）：messages.db 无 thread 列、raw_json 全空 → producer 推断靠文本头模式；x_monitor 卡的推文 URL 在按钮/零宽锚里不落存储文本 → L2 只能语义匹配。

- **DeliveredIndex**（sqlite `~/chat-daily/state/delivered_index.db`）：ingest_new（sync 群 → 增量入库，失败用陈旧索引）→ backfill_embeddings（批量，cap 200）→ prune（14d）；register_sent 在发送成功后复用闸门算过的向量（同轮撞车可兜住）
- **两级判定 TopicDedupGate**：Stage 1 批量 embed + cosine top-3 ≥ `candidate_min_sim`（0.80 占位，校准定）；Stage 2 SameEventJudge（terra@vibekey，≤5 次/轮，timeout 25s）输出 `{same_event, new_info: none|minor|substantial}`，解析链 fence 容错 + 枚举 coerce **默认 substantial=照发**
- **行为分级**（7-11「评论是增量」的延续）：非同事件→照发；substantial→照发；minor→🔁 标注 + `t.me/c/4424841223/<msg_id>` 深链；none（纯复读）→ enforce 跳过 / annotate 标注 / report 只记 journal。**无 judge verdict 永不 skip**（预算尽/judge 挂 → sim≥0.93 标注否则照发）
- **闸门位置**（集成时接线）：raw_channels 发送循环 seen→L1→L2→send（捕获 message_id）→write-after-send；private_media 同位（科技圈走私有路径——头号对撞方在这条路上）；`topic_gate=None` 参数默认零行为变化
- **Ratchet**：report ≥1 周 → annotate → enforce，一级不跳；日报 digest v1 豁免（入索引但 exclude 出检索）
- 成本：~150 万 token/月 embedding（≈$0.2-0.3 或免费档 0）+ judge 常态 <5 次/天封顶 40（<¥5/月）；全链路 <¥10/月
- **前置阻塞**：tg-cli 会话 = Congee 小号（@Congee123, id 8113034240），通知群里没有它 → 用户需拉号入群，校准与 ingest 才有数据面（`scripts/calibrate_topic_dedup.py` 第一步 fail-loud 探测）

## 4. Phase 4 — bwg 反向闸（macrumors-after-科技圈）

Mac 推 bundle、bwg 消费（与 sync_tg_targets 同方向，远端只读派生物）：`push_delivered_bundle.sh` 导出近 72h（norm_text+embedding+ts+producer+msg_id）scp+mv 原子替换 `/root/x_monitor/.delivered_bundle.json`，失败 exit 0；x_monitor 仓新增纯 stdlib `delivered_gate.py`（Gemini REST embed，GOOGLE_API_KEY 为 bwg 新 secret 记 runbook；judge 同 prompt 同解析链）。v1 只闸 macrumors_daily（日频）；X 推文侧 30min 节奏对新鲜度敏感，观察项。排期在 L2 report 模式出一周战绩之后，优先级由校准的方向性数据定。

## 5. Blindspot pass 归档（6 agent workflow，39→11 条 findings + 6 条 meta）

### Findings（severity | disposition）

1. **P0 | measure-first｜工具错配**：L1x 精确层语料上限 ~1 条/5 个月，抱怨主体是 L2 + 推送量（5 天涨 3.5 倍）；macrumors↔科技圈同落 thread 41 且无共享键空间，精确层结构性够不着。→ 已按此执行：gate 预写、度量、NO-GO。
2. **P1 | design-change-now｜抑制全群级 vs 阅读分线程级**：索引不记送达 thread（thread 丢失静默回落 General 照记 ok）；跨线程抑制越过注意力边界。→ 用户拍板「全群一面」，风险知情接受；L2 标注动作天然缓解。
3. **P1 | design-change-now｜裸链接分类器三处语料缺陷**：尾随全角/半角括号存活致设计自证案例对不上；`_URL_RE` 吞中文正文（46 处）；<30 字英文校准误杀中文策展。→ 全部修入 content_seen.py，06-27 对为验收 fixture。
4. **P1 | design-change-now｜标注优于抑制**（三 lens 独立收敛）：裸链接承载策展信号+是频道评论区唯一入口。→ 用户拍板 L1 维持跳过；L2 层吸收了此建议（minor→标注、无 verdict 不 skip）。
5. **P1 | measure-first｜度量效度四陷阱**：时间序（x_monitor 常态最后推）、索引健康断言、度量路径硬只读、fming 入组复测。→ 全部落进度量脚本。
6. **P1 | guard-in-v1｜误抑制永久不可见**：seen+HWM 终态写、决策证据自毁。→ journal + 日报计数 + --resend + 归档覆盖（发送循环内抑制的卡已在归档）。
7. **P2｜assumed_delivered 幽灵条目**：L1x 不建后无消费方；XMonitorIndex 休眠代码已含 assumed 跳过逻辑，翻案即生效。
8. **P2｜副本年龄上限**：XMonitorIndex >24h 视为不存在（四种根因统一落安全方向）。
9. **P2｜键空间缺口**（article 壳推、QT 壳/被引不对称）：共享映射函数已统一度量与运行时；度量脚本记 unmatched URL。
10. **P2 | accept-risk｜无 pending 态**：defer-one-cycle 会被 HWM 静默吞帖 → 记入「禁用的缓解手段」，同轮撞车显式接受。
11. **P2 | separate-project｜pairwise 拉取不可泛化**：由「交付面反推」底座（L2 DeliveredIndex）替代——单一底座覆盖全部 producer，Phase 4 也只是同一底座的镜像分发。

### Meta 观察

1. 标注 vs 抑制：三个 lens 从不同方向收敛到「标注是安全侧」；系统硬规则（宁可重复不可误杀）下抑制是风险最高的干预。
2. 非对称状态拓扑：不可逆的永久存储（seen+HWM）被会自毁的信号（GC 索引、快照副本）驱动——v1 必须打破永久性（journal/归档/resend）或给信号加置信度。
3. 顺序依赖是结构性的：单向方案只在 x_monitor 严格先推时有效，而它常态最后推；membership join 看不见顺序 → 度量必须时间序分桶。
4. 语料几乎在写码前解决了问题：真正查了 messages.db 的两个 lens 产出决定性事实，纯架构推理的 findings 三分之一被语料杀掉。**便宜的本地度量应先于传输设计。**
5. 防御投资倒挂：既有防御全护「允许的方向」（重复照流），新增抑制路径造出「禁止的方向」（静默丢失）却无对称防护——guards 三件套为此而设。
6. 跨 lens 缓解冲突：一个 lens 的直觉修法恰是另一个 lens 证明的坑（defer→HWM 陷阱、strip-CJK→中文路径 URL）——设计文档要记禁用清单。

## 6. 交付物清单（as-built）

| 文件 | 状态 |
|---|---|
| src/chat_daily_tg/content_seen.py + tests（57） | 已实现，L1 + 休眠 XMonitorIndex |
| src/chat_daily_tg/topic_dedup.py + tests（54） | 已实现，L2 全链 fail-open |
| src/chat_daily_tg/dedup_journal.py | 已实现，L1/L2 共用 |
| src/chat_daily_tg/paths.py | STATE_DIR 等常量已加 |
| scripts/measure_cross_producer_dup.py | 已跑，NO-GO 判决 |
| scripts/calibrate_topic_dedup.py | 已实现，阻塞在群成员资格 |
| 集成接线（config/raw_channels/private_media/run_daily） | 待办：工作树有另一任务未提交改动，接线时机待用户定 |
| Phase 4（bwg） | 依校准数据排期 |
