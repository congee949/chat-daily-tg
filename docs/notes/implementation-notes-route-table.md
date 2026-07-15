# Implementation Notes — 统一路由表 + LLM 输出校验（2026-07-09）

对应两份 spec：
- docs/spark/2026-07-09-unified-tg-route-table-design.md
- docs/spark/2026-07-09-llm-output-validation-design.md

只记录与 spec 有实质出入的决策 / 偏离 / 取舍，不复述实现。

## Design Decisions

- **cc98 forum topic = thread 1079**：用 Bot API createForumTopic 在群 -1004424841223 新建名为 "CC98" 的话题，thread_id=1079，已登记进路由表 `cc98` key。
- **路由表新增 3 个 key**：`market_recap=17`（与 chat_daily 共用现有 thread）、`growth=497`、`cc98=1079`。已通过 scripts/sync_tg_targets.sh 同步到 r4s、bwg（Mac 为唯一事实源）。
- **r4sbot 只迁 daily/want-watch**：`latest`/`search` 等按需命令的 --send 回复仍留在 DM（它们与 daily 共用 main 底部发送路径，用 `args.command == "daily"` 分流）。poll 交互回复不变，仍走 DM。
- **deliver_content 兜底阶梯**：cc98 群话题(带 parse_mode) → 群话题退纯文本(markdown 400 常见) → DM。want-watch 靠返回值（非异常）判断是否落 seen，保留「发失败不标记已见、下趟重试」语义。
- **x_monitor 用 overlay 而非改各发送点**：在 cfg 载入后调用 `apply_route_overlay(cfg)`，把路由表值覆盖进 cfg 的 telegram_group_chat_id / telegram_*_thread_id，下游发送代码零改动。今天是 no-op（表值与 config.json 完全一致，已 before==after 验证），只把事实源从 config.json 换成路由表。
- **health_check 改读 alert key**：优先路由表 `alert`(=10)，回落 config 的 health_alert_* 字段。功能等价，只为单源一致。

## Deviations

- **market-recap .env 保留 TG_CHAT_ID/TG_TOPIC_ID**：spec 写「.env 只留 token 与代理」，实际保留了 chat/thread 作为**回落安全网**——recap.sh 优先从路由表取，读不到才用 .env 旧值。理由：日报型 cron 不应因路由表缺失/损坏在 r4s 上硬失败漏发晨报，与全项目「rather deliver than drop」一致。属有意偏离，风险极低（正常路径已单源）。
- **x_monitor config.json 字段保留**：spec 本就把 config.json 定为「过渡安全网、保留不删」，此处与 spec 一致，非偏离，记此备忘。

## Tradeoffs

- **源码改动未提交**：run_daily.py / src/chat_daily_tg/vision.py 在本会话开始前已有未提交 WIP（`.get(x) or ""` 空值兜底等），与本次的 coerce_enum / resolve_tg_target / vision 改动**在同文件同区域交错**，非交互式无法干净拆分。故本地源码改动留在工作区不单独提交，由用户随分支 WIP 一起提交；仅两份 spec 文档已提交（5f7fea7）。
- **远程改动已直接落盘并各留 .bak-routetable-20260709 备份**：r4s(recap.sh, cc98_telegram_bot.py, cc98_health_check.py)、bwg(twitter_monitor.py, macrumors_daily.py, growth_digest.py) 均已备份+推送+py_compile 验证。这些在各自机器上，不在本仓库版本控制内。
- **未把 chat-daily-tg 源码改动部署到 r4s**：r4s 的 /root/chat-daily-tg 只跑 bilibili digest，其路由行为不受本次改动影响（仍读 bilibili=486），resolve_tg_target 的告警增强待用户提交+pull 后随部署生效。Mac 的 launchd 直接跑工作区文件，Spec ② 与 resolve_tg_target 告警下次 6:30 运行即生效。

## Open Questions

- 无阻塞项。可选后续：market-recap/health 的 config 回落字段在稳定后是否清理（当前保留更稳）。

## 验证记录

- Spec ②：281 tests 全绿；新增 vision（_normalize_score 越界归零、_coerce_include_flag、should_include AND 门）+ run_daily（coerce_enum、resolve_tg_target 三类回落告警）测试。
- Spec ①：sync --check 三机一致；market-recap 路由解析 =17；r4sbot content_target=(-1004424841223,1079)，deliver_content 探针实发 cc98 话题成功；bwg overlay before==after（no-op 验证）；6 个远程脚本 py_compile 通过。
