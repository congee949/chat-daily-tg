# Implementation Notes — Bilibili 订阅 digest（2026-07-02）

对照 spec：`docs/spark/2026-07-02-bilibili-subscriptions-design.md`（偏离已回写该文档）。

## Design Decisions

- **主数据源 feed → user-videos**：实测 `opencli bilibili feed` 输出只有昵称/标题/likes/相对时间/url，无 uid、bvid 字段、封面、精确时间，uid 白名单无法作用于它。改为逐个轮询白名单 UP 的 `user-videos <uid>`（uid 由查询参数确定），候选 bvid 再用 `video <bvid>` 补详情（含 mid、封面、精确 publish_time）。23 UP × 4 次/天 = 92 次调用，符合低频守则。
- **调度取 :30 偏移**（0:30/6:30/12:30/18:30，spec 原为整点）：避开频道转发器整点调度，防止两个 launchd job 同时争抢 opencli daemon / 代理。
- **topic 用 Bot API `createForumTopic` 直接创建**（bot 有 manage_topics 权限），名称「B站订阅」，thread_id 已写入 `~/qwenproxy/.tg-notify-targets.json` 的 `bilibili` key。
- **去重复用 `raw_seen.SeenStore`**（key `bilibili:<bvid>`，成功发送后才写入），文件 `~/chat-daily/bilibili_seen.txt`；不新建状态模块。
- **摘要复用全局 `models.vision`**（qwenproxy 封面理解），失败降级 `models.summary` 文本 LLM，再失败发无摘要卡片；不单设 `summary_model` 配置项。

- **观看链接改为 inline-keyboard 按钮**（2026-07-02 用户反馈）：caption 里不再放 🔗 文字链接，改为卡片下方「▶️ 在 B 站观看」URL 按钮（`TelegramSender.send_photo/send_card` 新增 `button=(text, url)` 参数，send_card 只挂最后一个 chunk）。`link_enabled` 语义随之变为控制按钮开关。

- **API 直连 transport（2026-07-02 晚，建议路径 step 1）**：为消除「每小时开关 Chrome 页面」的桌面依赖，新增 `transport: api`（默认）——medialist + view 两个接口实测零 cookie/零 WBI 签名（arc/search 会 -352 风控，故弃用），单 UP 一次调用拿齐全部字段，快 ~15 倍。关键约束：B站 httpx 请求 `trust_env=False`，绝不能走 guard 的 HTTPS_PROXY（海外出口 = 风控）。opencli 保留为 fallback。双端一致性已实测（相同 bvid 集合、字段一致）。
- **顺带修正 8 小时时区偏差**：对比发现 opencli `publish_time` 字符串是 UTC，旧卡片展示的发布时间一直偏早 8 小时；api 模式用 unix `pubtime` 转本地时间，正确。
- **可靠性收紧**：两条 transport 均新增「全部 UP 失败 → raise 告警」，防止 transport 挂掉后无限期静默零推送。

- **对抗式审查修复批（2026-07-03）**：多 Agent 审查（correctness / failure-modes / concurrency 三路完成；verify 阶段与 consistency 路撞订阅额度，findings 由主循环逐条对照代码人工核实）。已修：
  - P1 解析循环逐条隔离——单条脏数据（字符串 pubtime/毫秒时间戳/非法 duration）此前会击穿整轮抓取并引发每小时告警风暴，现单条跳过（`_parse_media_item` + per-item try）
  - P1 `cnt_info.play` int 收敛——脏值透传会在 `card_caption` 的 `f"{view:,}"` 炸掉整轮推送（caption 构建在 per-card try 之外）
  - P1 `download_cover` 补 `trust_env=False`——hdslb CDN 与 fetcher 同属"B站请求直连"不变量
  - P2 -352 风控止损——IP 级判决，首个 UP 命中即中止本轮（降频不绕过），不再对已风控 IP 连打 22 次
  - P2 connect 级瞬时抖动重试（`httpx.HTTPTransport(retries=2)`），防误触 all-fail 告警
  - P2 联合投稿去重——同一 bvid 出现在多个白名单 UP 空间时一轮只出一张卡（`_finalize` 内去重）
  - 记录不修（P2，与现有管道行为一致）：持续故障时告警无冷却（每小时一条）；opencli 路径 detail 阶段全灭不告警（已是 fallback）；手动运行与 launchd 并发无跨进程锁（launchd 同 label 不自我重叠）

## Deviations

- **Tier 2 完整视频摘要未实现**（spec §8 列为 P1 子项）：封面+标题+简介的一句话摘要实测已够决策用；下载视频+大文件多模态成本高，留作未来扩展。相应地 `summary_strategy` / `video_summary_up_whitelist` / `dynamic_type` / `up_source` / `cron` / `card_style` 等配置字段未落地——按最小 schema 实现。
- **user-videos 的 `date` 是天粒度**：粗筛用日期，精确过滤靠 detail 的 publish_time 二次判断（date 解析失败的条目保留，交给精确时间过滤）。

## Tradeoffs

- 轮询 user-videos（23 次/轮）vs feed 单次调用：多 22 次子进程调用换来 uid 精确匹配与稳定 schema；feed 的贫瘠输出无法支撑白名单语义，没有真正的替代路径。
- dry-run（--no-push）在下载封面/调 LLM 之前短路：省成本，但 dry-run 看不到摘要内容——验证摘要要真实发送。

## Open Questions

- **冷启动未实测**（§14.6）：Chrome 完全退出状态下 `--window background` 能否工作未验证（不便中断在用的 Chrome）。`probe_bridge()`（`opencli doctor` 探活）+ 48h lookback 已兜底：bridge 不在时告警退出、下轮追回。建议某次 Chrome 关闭时手动跑一次 `--bilibili-only --no-push` 确认。
- 首轮实测 48h 只有 5 条新视频，`max_per_digest: 30` 上限宽松；如后续觉得吵，调小该值或收缩白名单即可。

## r4s 迁移（2026-07-03，用户选定 bwg tailscale 隧道方案）

- **出口**：bwg 装 tinyproxy（EPEL `--enablerepo` 一次性启用，repo 保持 disabled），绑 tailscale IP `100.87.113.14:8888`，ACL 100.64.0.0/10，仅 CONNECT 443。tailscale 即隧道，无 ssh 转发守护。
- **musl 两坑**：无 venv 模块（pip3 --user 替代）；命名时区静默回退 UTC（cron 内 `TZ=CST-8` POSIX 格式，否则 8h 偏差复现）。pypi 直连超时，走清华镜像。
- **摘要分叉**：r4s config 的 models.vision → Gemini 多模态直连；Mac config 保持 CLIProxyAPI（用户 7/2 已从 qwenproxy 切过去），两份 config 有意不同步。
- **切换顺序**：Mac launchd unload → seen 同步 → r4s cron（flock 防 cron 重叠——launchd 有同 label 防重入而 cron 没有）→ 受控真发验证 1/1（8s，Gemini 摘要 + bwg 推送 + 封面直连）。
- **防双跑**：installer 注释掉 bilibili label；回滚路径写入设计文档 §18.4。
