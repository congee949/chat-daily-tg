# Implementation Notes — sources.youtube（YouTube 订阅 digest）

2026-07-19。需求：仿 B 站 digest 实现 YouTube 订阅推送，Tier 1 白名单，部署 r4s。
用户明确：Mediastorm影视飓风移出、KevinFeng（英语内容）加入、后续为运动康复等
非科技簇预留独立汇报 topic 的扩展空间。

## Design Decisions

- **传输层选 RSS + Data API 补时长，不用 opencli / 不用订阅 API**。
  `youtube.com/feeds/videos.xml?channel_id=UC…` 免登录免 key 免 quota，r4s headless
  可跑；RSS 无时长字段，新候选合并进**一次** `videos.list`（1 quota 单位/50 条，
  `GOOGLE_API_KEY` 已在 r4s .env，B 站 digest 的 Gemini vision 在用）补
  duration/viewCount，顺便驱动 Shorts 过滤。
- **代理契约与 B 站完全相反，且都写进了模块 docstring**：B 站强制 `trust_env=False`
  直连；YouTube/googleapis/i.ytimg.com 必须吃 wrapper 的 `HTTP(S)_PROXY`（r4s 经
  bwg tinyproxy）。**部署干跑抓到一个真雷**：httpx 传显式
  `transport=HTTPTransport(retries=2)` 会绕过 `trust_env` 的代理挂载——首轮干跑
  12 频道全部 SSL EOF（静默直连撞 GFW），curl 与裸 httpx 过代理均正常才定位到。
  修复为 `_proxy_from_env()` 显式把代理装进 transport；测试
  `test_client_transport_carries_env_proxy` 锁住这条（B 站侧有对称的反向测试）。
- **独立模块，不与 bilibili 抽公共层**。youtube_digest.py 与 bilibili_digest.py
  存在有意的重复（~170 行）：改 YouTube 永远不可能回归在跑的 B 站管线；两边
  失败隔离/write-after-send 语义逐条对齐。
- **Shorts 过滤：`duration_seconds <= 180` 丢弃**（2024-10 起 Shorts 上限 3 分钟），
  配置项 `min_duration_seconds` 可调。`P0D`（直播/首映占位）按 0s 丢弃——上轮
  未 seen，VOD 出真实时长后自动进下一轮，等于"推正片不推开播预告"。
- **每频道 `topic` 字段预留簇路由**：`YoutubeChannel.topic`（None → digest.topic
  默认 `youtube`），`run_youtube` 按 effective topic 分组、每组独立 sender。
  未来英语学习/运动康复簇加频道时只改 config + 路由表，不动代码。
- **按钮直链 `youtube.com/watch`**：TG 客户端点开由系统 universal link 唤起
  YouTube app，无需 B 站那种 kanban.congeelife.top 跳转页。
- **cron 现为 scheme B**：`*/5` 探测 + `due_gate` 随机 10–15min（600–900s），
  成功后才 schedule 下一轮；失败 gate 保持 open 由下次 */5 重试。due_gate 必须在
  hb-wrap 之前，避免 skip tick 假绿心跳。B 站同模式 20–30min（1200–1800s）。
- **write-after-send to media_sent_ledger for Podcast 👍 handoff**（mirror
  bilibili）：每张卡成功发送后 `append_message_ids(..., producer="youtube",
  content_id=video.seen_key, url=video.url)`。caption 为美观不再打印 URL；
  URL 只在 watch 按钮 + ledger 上（handoff 经 ledger 解析）。ledger 写失败只
  `log.warning`，不阻断 seen/下一张卡。生产环境需 redeploy r4s 上的
  chat-daily-tg 后 YouTube 卡才会写 ledger。

## Deviations

- 上一轮白名单结论中的 **Mediastorm影视飓风移出、KevinFeng 加回**（用户本轮
  指示；KevinFeng 即当前的英语学习位）。最终 12 频道全部从用户真实订阅页
  （feed/channels，99 订阅）提取 channel_id，并逐一经真实 RSS 回验 feed 标题。

## Tradeoffs

- **Shorts 在 enrichment 失败时只剩 `#shorts` 标题启发式**——投递优先于完美：
  时长未知仍推送，宁可偶尔混进一条短片也不因 quota/网络问题空推。被过滤的
  Shorts **不写 seen**（避免无 journal 的无发送 seen 写入），代价是 48h 窗口内
  其 id 反复进批量 enrichment 调用（1 quota 单位，可忽略）。
- **RSS 只有最近 15 条**：某频道两轮间隔内发 >15 条会漏尾部——对本白名单
  （长视频低频向）不可能触发，不做分页。
- 低频频道（不良林 2026-01、Logan 2026-03、Scott Yu-Jan 2026-04 最后更新）保留：
  用户核实过的"低频硬核"定位，RSS 轮询成本为零。

## Open Questions

- 英语学习簇目前只有 KevinFeng 一个频道、走默认 youtube topic；若之后要独立
  `english` topic（同运动康复计划），建话题 + 路由表加 key + 该频道 config 加
  `topic: english` 三步即可。
- 运动康复簇（订阅里 ~15 个 PT 频道）未筛未入白名单，等用户点名再加。

## 2026-07-21 补丁：YouTube RSS 平台侧抖动 → 每频道 feed 重试

- **现象**：10:50 / 12:40 / 13:10 三波 `YoutubeFetchError: all 11 channel feed
  fetches failed — transport dead?`，几分钟后自愈。r4s 日志显示真实错误是
  **404 Not Found（偶发 500）**，不是 TLS/代理断——同一频道直连 404、走 bwg
  代理 200，重试几次即恢复 200，且 404 响应有时仍带合法 feed body。
- **定性**：YouTube `/feeds/videos.xml` 自 2025-12 起的平台侧间歇故障
  （RSS-Bridge#2113、FreeTube#8443、n8n/Google AI 论坛多帖；高峰时段、
  数据中心出口 IP 更容易中招），随机频道、随机时间窗 404/500。
- **修复**（`youtube_fetcher.py`）：新增 `_fetch_feed_with_retry`——每频道
  最多 `_FEED_ATTEMPTS=3` 次，退避 3s/6s，仅对
  `_FEED_RETRYABLE_STATUSES = {404, 429, 500, 502, 503, 504}` 与传输层异常重试；
  其他 4xx（如 403）立即失败。单频道重试耗尽才算 failure，all-fail 告警语义
  不变。最坏情况多耗时 ~100s，flock 保证不与下一轮重叠；漏抓的视频由
  lookback 窗口在下一轮补回，不丢。
- **测试**：`test_single_channel_failure_does_not_kill_run` /
  `test_all_channels_failing_raises` 改 `is_reusable=True` + monkeypatch 退避为
  0；新增 `test_flaky_feed_recovers_on_retry`（首试 404、重试 200 必须正常出片）。
  全套 563 passed。
- **验证**：r4s 实跑 `run_daily.py --youtube-only` exit=0——坏窗口内仍有个别
  频道三次全 404/500（单频道降级跳过，符合设计），但整体不再硬失败、不告警。
- **已知残留**：`GOOGLE_API_KEY` 仍 403（console 限制在 Gemini API，见
  `_enrich_via_watch_page` docstring），enrichment 走 watch-page 兜底，Shorts
  过滤不受影响。

## 2026-07-21 补丁 2：YOUTUBE_API_KEY 拆分，Data API enrichment 上线

- 用户新建只放行 YouTube Data API v3 的 key；r4s `config.yaml`
  `sources.youtube.api_key_env: GOOGLE_API_KEY → YOUTUBE_API_KEY`，`.env` 新增
  `YOUTUBE_API_KEY`（旧 `GOOGLE_API_KEY` 保留给 Gemini vision，两 key 不互换）。
- 验证：3Blue1Brown《But what is cross-entropy?》走真实管线推送测试卡——
  API enrichment 命中（33m51s / 392,919 views，与 videos.list 直连结果一致），
  TG 卡片发送成功并写 seen。watch-page 兜底保留作降级。

## 2026-07-21 补丁 3：多分钟全灭风暴 → 第二波 + 告警节流

- **现象**：下午 14:45–14:57 在已上线 per-channel 3 次重试后，仍连续三轮
  `all 11 channel feed fetches failed`（真实错误仍是 404/500，非代理）。
  单频道 ~9s 重试窗口扛不住持续约 12 分钟的全频道风暴；due_gate */5 重开
  导致 python `notify_failure` + shell `alert` 双子告警连发。15:00 自愈。
- **修复**：
  1. `_fetch_feeds`：首波全灭时 sleep `_FEED_WAVE_BACKOFF_SECONDS=45` 后对失败
     频道再跑一波（`_FEED_WAVES=2`）；部分恢复即放行，仍全灭才 raise。
  2. `run_daily.run_youtube` + `run_youtube_r4s.sh`：同类失败 TG 告警 20 分钟
     节流（日志仍每轮记），避免风暴期刷屏。
- **测试**：`test_all_fail_first_wave_recovers_on_second`；原 all-fail 用例改
  为覆盖两波。
