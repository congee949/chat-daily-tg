# Implementation Notes — Bilibili 订阅 digest（2026-07-02）

对照 spec：`docs/spark/2026-07-02-bilibili-subscriptions-design.md`（偏离已回写该文档）。

## Design Decisions

- **主数据源 feed → user-videos**：实测 `opencli bilibili feed` 输出只有昵称/标题/likes/相对时间/url，无 uid、bvid 字段、封面、精确时间，uid 白名单无法作用于它。改为逐个轮询白名单 UP 的 `user-videos <uid>`（uid 由查询参数确定），候选 bvid 再用 `video <bvid>` 补详情（含 mid、封面、精确 publish_time）。23 UP × 4 次/天 = 92 次调用，符合低频守则。
- **调度取 :30 偏移**（0:30/6:30/12:30/18:30，spec 原为整点）：避开频道转发器整点调度，防止两个 launchd job 同时争抢 opencli daemon / 代理。
- **topic 用 Bot API `createForumTopic` 直接创建**（bot 有 manage_topics 权限），名称「B站订阅」，thread_id 已写入 `~/qwenproxy/.tg-notify-targets.json` 的 `bilibili` key。
- **去重复用 `raw_seen.SeenStore`**（key `bilibili:<bvid>`，成功发送后才写入），文件 `~/chat-daily/bilibili_seen.txt`；不新建状态模块。
- **摘要复用全局 `models.vision`**（qwenproxy 封面理解），失败降级 `models.summary` 文本 LLM，再失败发无摘要卡片；不单设 `summary_model` 配置项。

- **观看链接改为 inline-keyboard 按钮**（2026-07-02 用户反馈）：caption 里不再放 🔗 文字链接，改为卡片下方「▶️ 在 B 站观看」URL 按钮（`TelegramSender.send_photo/send_card` 新增 `button=(text, url)` 参数，send_card 只挂最后一个 chunk）。`link_enabled` 语义随之变为控制按钮开关。

## Deviations

- **Tier 2 完整视频摘要未实现**（spec §8 列为 P1 子项）：封面+标题+简介的一句话摘要实测已够决策用；下载视频+大文件多模态成本高，留作未来扩展。相应地 `summary_strategy` / `video_summary_up_whitelist` / `dynamic_type` / `up_source` / `cron` / `card_style` 等配置字段未落地——按最小 schema 实现。
- **user-videos 的 `date` 是天粒度**：粗筛用日期，精确过滤靠 detail 的 publish_time 二次判断（date 解析失败的条目保留，交给精确时间过滤）。

## Tradeoffs

- 轮询 user-videos（23 次/轮）vs feed 单次调用：多 22 次子进程调用换来 uid 精确匹配与稳定 schema；feed 的贫瘠输出无法支撑白名单语义，没有真正的替代路径。
- dry-run（--no-push）在下载封面/调 LLM 之前短路：省成本，但 dry-run 看不到摘要内容——验证摘要要真实发送。

## Open Questions

- **冷启动未实测**（§14.6）：Chrome 完全退出状态下 `--window background` 能否工作未验证（不便中断在用的 Chrome）。`probe_bridge()`（`opencli doctor` 探活）+ 48h lookback 已兜底：bridge 不在时告警退出、下轮追回。建议某次 Chrome 关闭时手动跑一次 `--bilibili-only --no-push` 确认。
- 首轮实测 48h 只有 5 条新视频，`max_per_digest: 30` 上限宽松；如后续觉得吵，调小该值或收缩白名单即可。
