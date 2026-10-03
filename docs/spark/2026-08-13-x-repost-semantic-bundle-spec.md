# Spec: X Repost Selection Trigger and Semantic Bundle Delivery

日期：2026-08-13

状态：实施规格

范围：BWG `/root/x_monitor` 的 X GraphQL 规范化、筛选、跨账号去重、Telegram 渲染、seen/retry/decision 状态；本仓库仅保存设计与验收记录。

## 1. 决策

Repost 是选品触发，不是推送正文。

从 monitored account 时间线观察到帖子后：

1. 连续穿透 `repost_of`，第一个非 repost 帖子成为 content anchor。
2. anchor 决定推送主体、主作者、主链接和 bundle 去重身份。
3. anchor 的 Quote、媒体和 Article 是理解它所需的 context；按有界关系图展开。
4. 外层 repost 只保留为 `observed_via` / `repost_path`，可显示一行轻量来源，不显示 `RT @...` 壳。
5. 筛选发生在 semantic bundle 解析完成之后；不得先用残缺的外壳或短评论过滤。

例：`@dotey repost @Gorden_Sun quote @Arcadia_Bao(image)`：

- selection trigger / observation：`@dotey` 的 repost；
- anchor：`@Gorden_Sun` 的“草，太传神了”；
- context：`@Arcadia_Bao` 的“大家都是 fable 级”和所属图片；
- Telegram 主体署名 `@Gorden_Sun`，可标“经 @dotey 转发发现”，图片署属 `@Arcadia_Bao`；
- 主按钮打开 Gorden 的 anchor，不打开 dotey 的 repost 壳。

官方即时告警策略仍可拒绝纯 RT。是否允许 repost 触发由账号 policy/role 决定，不能全局覆盖现有 official original-only gate。

## 2. 真实故障定位

2026-08-13 对生产 BWG 只读核查：

- @dotey 外壳 `2087708053009273250` 被推送；规范化结果只有 `RT @Gorden_Sun: 草，太传神了`、`retweeted_status=B`、`media=[]`。
- 直接规范化 B `2087700436036079825` 只有 `quoted_status=C` 的 ID/作者，仍无 C 正文与图片。
- 原始 GraphQL 数据实际包含完整的 A→B、B→C 关系和 C 的图片。丢失发生在 `twitter_graphql.py` 的扁平 normalizer，而非 X API 或 Telegram。
- `twitter_graphql.py` 只解析 outer 的一层 RT/Quote；RT 仅回填 B 自身媒体，未解析 B 的 `quoted_status_result`。
- `format_message()` 只渲染顶层 `text/media`，header 使用 monitored account；下游已无法恢复 C。
- `process_user()` 在 context resolution 之前 classify。若仅改显示主体，B 的六字评论会命中 `too_short`，导致错误漏推。

## 3. 身份模型

必须分开四种身份：

### 3.1 Observation

时间线上真实观察到的外壳：

```text
observation_key = x:<observed_via>:<outer_tweet_id>
```

只有 observation 参与该 monitored account 的 source seen 生命周期。

### 3.2 Content anchor

从 observation 开始只沿 repost 连续穿透后，第一个非 repost 节点：

```text
anchor_key = t:<anchor_tweet_id>
```

它是筛选、投递、主链接、claim 和 bundle dedup 主体。

### 3.3 Context node

anchor 的 Quote/Article/媒体及为理解它所需的递归引用。Context 不改变 anchor；detail fetch context 绝不能推进任何账号的 source seen。

### 3.4 Asset

媒体和 Article 必须保留 owner：

```text
t:<tweet_id>
a:<article_id>
m:<stable_media_id_or_hash>
```

选品者不获得作者权威，也不拥有被引媒体。

### 3.5 Alias

- 纯 repost path 中的 shell tweet IDs 是 anchor aliases。
- Quote 永不 alias 到被引帖。
- B Quote C 与 C 原帖是两个 bundle；B1/B2 分别 Quote C 也不能因共享 C 而互相去重。
- Article 可另有内容索引，但 delivery bundle 仍追溯到 anchor tweet。

## 4. SemanticBundle schema

```text
schema_version
observation
  outer_id / observed_via / observed_at / source_url / fetch_mode
anchor
  tweet_id / author / text / note / source_url / created_at / metrics
repost_path[]
  tweet_id / author
context_nodes[]
  tweet_id / author / relation / parent_id / text / source_url / completeness
assets[]
  asset_id / owner_tweet_id / kind / url / dimensions / duration / completeness
article_refs[]
  article_id / owner_tweet_id / title / body_ref / completeness
identity
  bundle_key / alias_keys[] / context_keys[] / article_keys[]
resolution
  status / required_context_complete / depth / node_count / request_count / reasons[]
classification
delivery
```

`alias_keys` 与 `context_keys` 不得混用。Metrics 必须 node-scoped，并带抓取时间。

## 5. 解析算法与预算

1. 优先递归使用 timeline payload 已嵌 raw nodes，避免额外请求。
2. 沿 repost 找 anchor；repost shell 不贡献正文，只进入 lineage。
3. anchor 冻结后解析 Quote；context 内遇 repost 可继续穿透，但不改变根 anchor。
4. 只有缺失 required edge/node 时才按 tweet ID detail fetch。
5. 同一轮按 tweet ID single-flight；跨账号共享缓存。
6. 每个媒体和 Article 挂到真实 owner node。
7. 解析结束后才分类完整 bundle。

硬限制：

- relation depth ≤ 3；
- nodes ≤ 5；
- 每 bundle 额外 detail requests ≤ 2；
- 每轮 detail requests ≤ 12；
- resolver soft deadline 90 秒；
- visited tweet-ID 防环；tweet ID 只接受有限长度十进制；
- 429 只尊重有上限的 `Retry-After`，不绕过、不无限重试。

建议缓存：complete 24h、partial 15m、transient negative 5m、deleted/protected 24h；损坏缓存转重抓，不得伪装 complete。

## 6. Completeness 与决策

至少区分：

```text
complete
degraded_optional
context_unavailable_terminal
context_unresolved_transient
context_unresolved_expired
truncated_budget
cycle_detected
schema_drift
auth_degraded
```

字段缺失不等于关系不存在。authenticated、guest、fallback 必须记录 source/fetch mode/completeness。

若 anchor 明显依赖 context（如“草，太传神了”“this”“看这个”）：

- transient：defer，不写 seen，进入 bounded retry；
- terminal：不发送残卡；先写 suppression decision journal，再写 outer seen；
- 超过 freshness/retry budget：写 `context_unresolved_expired` journal 后终态 seen。

若 anchor 本身可独立理解，context terminal unavailable 时可降级发送，并明确标注。媒体增强失败必须降级为正文和原链接，不能阻塞主体投递。

## 7. 筛选和账号策略

- Curator 账号允许 repost 作为 candidate trigger；selection signal 可参与兴趣/价值判断。
- Official original-only policy 保持纯 RT fail-closed。
- 分类输入必须包含 anchor、required context、媒体/Article 类型和 observed_via；不能把 shell 的 `RT @` 当内容长度。
- LLM 输出的结构、enum、required-context 判断必须有 code-level 校验、归一化和回退。
- 6551/fallback 若不能返回 repost，必须显式标 producer degraded，不能报告为健康且“零选品”。

## 8. Telegram 投影

推荐结构：

```text
📢 @Gorden_Sun
经 @dotey 转发发现

草，太传神了

↳ 引用 @Arcadia_Bao
大家都是 fable 级

[C 所属图片]
[查看原帖] [查看被引原帖]
```

规则：

- 主 header、正文和主按钮属于 anchor。
- 不显示 repost shell 的 `RT @...` 正文。
- Quote context 分块展示；媒体紧随 owner block。
- Rich → representative photo → HTML/text 的现有 delivery-first 降级保留。
- 所有媒体失败仍须发送 anchor/context 正文与链接，并记录 `media_degraded`。
- 一次 bundle 若分成多个 required Telegram parts，全部成功才 confirmed；可选增强失败不阻主体。

## 9. Seen、retry、ledger 与 journal

```text
fetched -> resolving -> candidate -> filtered_terminal | duplicate_terminal | pending_send
resolving -> context_unresolved_transient -> retry_pending
resolving -> context_terminal/expired -> suppressed_terminal
pending_send -> confirmed | ambiguous | failed_pre_send
failed_pre_send -> retry_pending
```

不变量：

- source seen 只写 outer observation ID；detail-fetch 的 anchor/context 永不写独立 seen。
- confirmed 才 write-after-send；明确 send failure 不 seen，进入 retry。
- ambiguous 沿用现有 assumed-delivered/ledger 语义：checkpoint outer seen，绝不盲目第二次 POST。
- filtered、duplicate、stale、context terminal 只有 append-only decision journal 成功后才允许 seen。
- journal 至少包含 observation、anchor、aliases、resolution、reason、matched delivered bundle、resolver/render version。
- delivery index 按 `(target chat/thread, anchor_key)` 原子 claim；confirmed/ambiguous 后登记。
- `--test`、`--dry-run` 不污染生产 delivery index；seed 只写 observation seen 并记录明确原因。

## 10. 跨账号去重真值表

- A repost B 先送达，随后直接观察 B：不重复。
- B 先送达，随后 A repost B：不重复。
- 多个账号 repost B：只投递一次，审计可合并 `discovered_via`。
- A repost B Quote C 与直接观察 B：同一 bundle `t:B`。
- B Quote C 与 C 原帖：不同 bundle，均可候选。
- B1 Quote C 与 B2 Quote C：不同 anchors，均可候选。
- 后续编辑/补媒体不得靠同 tweet ID 静默重发；记录 revision/hash，由明确 update policy 决定。

旧 `.pushed_index.json` 已使用纯 RT 原推 `t:B`，新 bundle index 必须双读/兼容 v1，避免上线重灌历史。

## 11. 可观测性与安全

每个 observation 必须留下结构化决策：resolver version、outer/anchor/context IDs、fetch mode、completeness、requests/cache hits、classification、dedup match、delivery result、TG message IDs。

告警：任一 schema drift；连续三轮 auth degraded；resolver budget exceeded；decision journal/ledger 写失败。

安全：

- detail fetch 只构造固定 X GraphQL endpoint，不请求用户提供的 expanded URL。
- 媒体下载/探测限制可信 X media host；不得形成 SSRF。
- 认证 header 不得进入日志/异常；递归抓取不能扩大 cookie argv 暴露。
- 所有缓存与 journal 原子写、容量有界、权限合理。

## 12. 必须通过的测试

1. A repost B(text/image)：主体/按钮/媒体属于 B；A 只 observed_via；无 RT 壳。
2. 真实形状 A repost B Quote C(photo)：B 评论、C 正文、C 图片和 owner 全正确；inline 完整时零额外请求。
3. A repost B repost C：anchor C；A/B 仅 lineage。
4. A repost B Quote C Quote D：anchor B 不变，context 顺序/作者正确。
5. B Quote C 与 C 原帖互不抑制；B1/B2 Quote C 也互不抑制。
6. 多账号并发 repost B：原子 claim 后只发一条。
7. repost B 后直接观察 B，以及反向顺序，均不重复。
8. detail-fetch C 后 C 的账号 seen 不变；以后直接观察 C 仍可候选。
9. context terminal 且短 anchor 依赖它：journal + outer seen，不发残卡。
10. context transient/429/timeout：调用有界、outer 不 seen、恢复后可发送。
11. anchor 可独立理解而 context unavailable：降级发送并标注。
12. rich/photo/media 全失败：正文和 anchor/context links 仍成功投递。
13. auth/guest 公开帖 bundle 不等价时必须标 partial/auth_degraded，不得误判无关系。
14. cycle、depth 4、nodes 6、request over-budget：确定终止，无无限请求/错署名。
15. Quote/short-comment 在 context 加载前不得 `too_short` filter。
16. C 的 Article：C 署名/图片，B 仍是 anchor 评论者，tweet/article identity 均可审计。
17. definite send fail：outer unseen+retry；ambiguous：ledger+journal+seen、不重 POST。
18. dry-run/test 不污染 production index。
19. v1 pushed index 迁移不重发；context key 不被当 alias。
20. 20 observations 共享 context：每个缺失 unique node 本轮最多 fetch 一次，且不越预算。

既有一层 RT/Quote Article、视频/GIF、URL 展开、official policy、event ledger、ambiguous delivery 测试必须继续通过。

## 13. 性能与 rollout gate

生产基线（2026-08-13，最近 193 轮）：p50 25.0s、p95 39.0s、p99 48.7s、max 49.3s。

实现验收：

- embedded-only resolve p95 < 50ms；cache hit p95 < 10ms；
- detail calls p95 ≤ 2/candidate，每轮绝不超过 12；
- 随机/故障关系图均满足 depth≤3、nodes≤5、request budget；
- context/media 增强失败时正文投递降级成功率 100%。

部署前先 shadow 24h（只解析，不改变投递）：schema drift=0；样本 N≥100 后非终态候选完整率≥98%；budget exceeded<1%。

灰度 7 天：run p95≤90s、max≤300s、global timeout=0、overlap skip=0、429<1%；连续三轮 auth degraded 或任何 schema drift 告警。

## 14. 实施顺序与回滚

1. 抽取递归 node normalizer，先用 fixture 验证，不切生产行为。
2. 加 SemanticBundle resolver、typed completeness、budget/cache。
3. `process_user` 改为 resolve-before-classify；加入 context retry/seen 隔离。
4. renderer 消费 bundle；保持旧平面 renderer 为失败 fallback。
5. 引入 anchor-based claim/index 并双读 v1 pushed index。
6. 故障注入、property/performance tests。
7. shadow → 单 curator 灰度 → 扩大；official policy 不参与首轮灰度。

回滚只切 feature flag 回旧路径；不得删除新 journal/cache/ledger，也不得重置 seen。回滚不得触发历史重发。

## 15. 实现决策补充（2026-08-13 第三轮）

- provider normalization 永远只做 embedded-only build；只有 shadow 或 curator gray 才允许 detail resolve。因此 feature off 不产生 detail 请求，builder 异常也只附加诊断并保留旧平面字段。
- detail resolver 对单 bundle 最多 2 次、单轮最多 12 次、总 deadline 90 秒；HTTP/GraphQL 429 读取 `Retry-After`（秒或 HTTP-date），上限 30 秒，只重试一次并设置整轮 cooldown，避免换 ID 连打。
- detail cache 使用原子 JSON、0600、最多 500 项并按 typed TTL 清理，可跨 cron 复用；terminal、transient、complete 分开缓存。
- retry 文件采用兼容旧 list/dict 的 v2 records，持久保存 `first_deferred_at`、`outer_created_at`、`attempts`。掉出 timeline 后可由 outer ID 合成恢复；达到 6 次或首次 defer 超过 24 小时才进入 expired，且必须 journal-before-seen。
- shadow ledger 是独立、append-only、有界轮转的结构化 JSONL。shadow 开关不能改变 send、seen、retry、pushed index、decision journal；只有 ledger 可不同。
- gray 必须同时满足 feature flag 与非空 curator allowlist；6551 fallback 和 GraphQL guest/auth-degraded 不进入 semantic delivery。它们继续走旧路径并保留显式降级来源。
- Article 内容资产仍按 article ID 复用；投递 gate 对 semantic Article 使用 `ab:<bundle_key>`，使共享同一 Article 的不同 quote anchors 保持独立。队列同时持久化文章 owner 和 `comment_author`，渲染引子署 anchor 作者。
- classification 以 anchor 与全部 contexts 合并成 validated view，携带 URL、hashtag、media、Article 字段，再复用正式 classifier。这样正常长度 anchor 也不能掩盖 context 中的 affiliate、商业自曝或禁用 hashtag 信号。

### 第四轮故障语义补充

- shadow resolver 与 ledger 各自 fail-open；任一异常都回到同一 legacy classification/send/seen/index 路径。shadow 开启时 provider 的每条输入都写 observation，包括无 bundle、builder/resolver exception 与 6551 fallback，避免只统计成功解析样本造成分母偏差。
- shadow observation 显式记录 account、source/fetch mode、legacy/semantic decision、exception、每条及本轮 physical attempts、cache、request budgets、deadline/cooldown remaining 与 latency。ledger 写失败只告警，不阻断正文。
- 单轮 12 次预算按 physical HTTP attempts 计数。首次 429 后只允许一次 retry；sleep 后必须重新检查 90 秒 deadline 和 run budget。retry 再次 429 时从第二次响应的当前 monotonic 时刻重新设置（最长 30 秒）run cooldown，后续其他 ID 被 latch 阻断。
- detail endpoint 使用 guest token 时向 resolution 传播 `graphql_guest_detail`。即使 outer timeline 是 auth，混入 guest detail 也标记 `auth_degraded`，semantic classification 为 defer，gray delivery 回到 legacy 路径。
- semantic 正式分类视图对每个 node 使用 NoteTweet 优先，并先按 entity 展开 t.co；之后再合并 anchor/context 并复用正式 affiliate/hashtag/commercial/article 规则。
- v1 Article index 迁移按编辑语义区分：无实质 quote comment 继续使用 `a:<article_id>` 并受旧 index 抑制；有实质 anchor comment 使用 `ab:<bundle_key>`，同一文章的不同评论可分别投递。

### 第五轮最终决策与预算顺序

- shadow ledger 同时保存 `pre_ai_classification` 与 `final_classification`；append 发生在 AI 复核或无 AI fallback 已经确定之后。每个 provider input 恰好一条 observation，不能在 resolver 与 classifier 两个阶段分别 fsync。
- terminal relation 的事实优先于 guest transport：terminal/degraded-terminal status 不被 `auth_degraded` 覆盖，`fetch_modes` 仍保留 guest 来源；gray gate 看到任何 guest mode 依旧回退 legacy。
- detail budget 只服务待处理的新 unseen observation 与 durable `push_retry`。已 seen observation 不调用 resolver/detail、不调用 AI、不改变 send/seen/index；shadow 开启时只基于 provider 已嵌入关系写一条 `duplicate_seen_embedded_only` observation，使其可从 unique candidate 分母排除。
- 缺失 tweet ID 的 provider row 也写一条 `invalid_input` shadow observation，但不进入 resolve/classify/send 状态机。所有异常与 fallback 仍遵循每输入一记录。
