# 每日用电与睡眠技术实现

日期：2026-10-03。本文是 ChatDaily 增加小米插座用电与 HAE 睡眠呈现的实现依据。

## 目标

早上的 Daily Recap 直接展示前一个完整北京自然日的小米智能插座用电量，并在数据积累后提供周趋势与月趋势。睡眠只展示已经完整同步的昨夜记录；7:05 尚未同步时，日报照常发出并标明待补，数据到达后只补发健康段。

## 已核实边界

- 日报由 `com.chat-daily-tg.agent` 在 7:05 触发，`scripts/run_daily_guarded.sh` 调用 `run_daily.py --wait-for-wake`。当前 `wait_for_wake_signal()` 只探测一次，没有睡眠数据时立即继续。
- 健康段由 `build_health_report()` 生成，经 `format_health_briefing()` 放到总结正文前面。健康段失败只记日志，不阻止日报。
- 小米智能插座 3 的日电量实体是 `sensor.cuco_v3_1a15_power_cost_today`，单位 kWh，`device_class=energy`、`state_class=total_increasing`。它在每天 00:04–00:05 归零，因此前一天最后一条有效读数才是当天用电量。
- 2026-09-25 至 2026-10-02 的完整日电量为 2.06、1.93、1.87、1.33、1.26、1.04、1.95、2.31 kWh。Recorder 当前只保留这段状态历史，不能回算更早的正式周报或月报。
- 月电量实体 `sensor.cuco_v3_1a15_power_cost_month` 只作交叉校验。它在 2026-10-01 00:04 归零，但 9 月内部有非月初回退，不能直接当月报账本。
- HAE 导出目录为 `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync`。`HealthExportReader.sleep_ending()` 已读到 2026-10-02 的 00:02–07:15、实睡 6.99 小时，以及 2026-10-03 的 00:05–07:45、实睡 7.41 小时。
- `sleep_analysis` 的常规文件在 2026-07-31 至 2026-09-27 缺失。7–10 月已生成的 62 份晨报中，只有 2 份使用“昨夜睡眠”，其余因同步时点落在日报之后而显示尚未同步。

## 用电设计

每日收盘值是用电账本的唯一事实来源。目标北京日期 `D` 的用电量，取 `D 00:00:00+08:00` 至 `D+1 00:10:00+08:00` 内最后一条可解析的非负读数。下一个北京日 00:10 前出现的归零值不能替代 `D` 的收盘值。

读取路径固定为 Mac 通过 `ssh r4s` 打开 Home Assistant Recorder：

`file:/opt/homeassistant/config/home-assistant_v2.db?mode=ro`

查询先从 `states_meta` 用完整实体 ID 取得 `metadata_id`，再读取 `states`。SQLite 使用只读 URI，不写 HA、不重启容器、不创建 Utility Meter。

本地账本保存到 `~/chat-daily/state/energy/daily.json`。每天的记录包含日期、收盘值和来源实体。写入使用临时文件和原子替换。重复运行同一天必须保持幂等。历史不足的日期保留空账，不从瞬时功率或月电量倒推。

展示口径：

- 日报展示目标日前一个完整日，例如 10 月 4 日早上展示 10 月 3 日。近 7 日均值至少有 3 个完整日才显示，并给出绝对增减。
- 周一额外展示上周一至上周日。七天齐全才称为完整周，输出总量、日均、最高日和最低日；上一周也完整时再输出增减。
- 每月 1 日展示上月总量、日均、最高日、最低日和每日明细。上月每天齐全才输出月环比。
- 数据源不可达、实体缺失或目标日没有收盘值时，用电段写明“用电数据暂缺”，日报继续。

周固定为周一至周日。由于 2026-09-28 之前没有收盘账，第一份完整周是 2026-10-05 至 2026-10-11，在 2026-10-12 的日报展示。第一份正式月报为 2026-11-01，汇总 2026-10。

## 睡眠设计

继续使用现有 HAE 读取器和 `Asia/Shanghai` 时区，不新增健康数据源。`wake_sleep` 必须结束于当天，才是“昨夜睡眠”；不能用更早的完整记录顶替。

7:05 的主日报保持立即发送。已有完整昨夜睡眠时，健康段显示起床时间、实睡、核心、深睡、REM 和清醒时长。没有完整记录时，明确写“昨夜睡眠尚未同步，稍后补发”，同时保留已经可用的活动数据。健康图和富文本只引用同一次报告中的睡眠状态，避免正文与图形互相矛盾。

补发是独立的轻量入口，不调用总结模型，也不重发群聊总结。它重新读取 HAE，只有在目标日的 `.health-card-sent` 尚不存在、且此时已有完整昨夜睡眠时发送一次健康段。成功后写入标记；重复运行直接退出。补发任务可以在主日报后的上午多次触发，但标记保证只有一次补发。

HAE 文件缺失、iCloud 占位尚未落地或解码失败时，沿用现有 gap 记录。连续的历史缺口不在本次修复中回补。

## 晨报卡片

用电和睡眠合成一张图片消息，在群聊日报之前发送，由 `telegram.morning_card`（默认开启）控制。实现在 `src/chat_daily_tg/morning_card.py`：`build_panel()` 汇总一次数据，图片与说明文字都从这份数据生成。

- 图片为 1080×1350 浅色竖版，上半部分是昨日用电和近 8 日柱状图（昨天高亮，虚线为前 7 日均值），下半部分是实睡时长、入睡和起床时间以及睡眠阶段分段条。
- 说明文字使用 Telegram HTML：标题、两行结论，以及一个 `<blockquote expandable>` 折叠块，内含均值、周/月汇总、睡眠阶段、活动与恢复数据。可见长度超过 1000 字时从末尾删减明细。
- 比较基准为前 7 日（D-7 至 D-1）均值，至少需要 3 天数据，文字版用电段采用同一口径。
- 卡片发送成功后，`.morning-card-sent` 记录消息 ID，日报正文去掉健康和用电前缀；渲染或发送失败时，日报保留原有文字。
- 睡眠待补时，`chat-daily-tg daily health-followup` 重绘卡片并用 `editMessageMedia` 替换原消息；编辑被拒（400）时改发一张新卡片。

## 文件所有权

用电实现只编辑：

- `src/chat_daily_tg/energy_usage.py`
- `src/chat_daily_tg/config.py`
- `src/chat_daily_tg/application.py`
- `tests/test_energy_usage.py`
- `tests/test_config.py`

睡眠实现只编辑：

- `src/chat_daily_tg/health_briefing.py`
- `src/chat_daily_tg/health_rich.py`
- `src/chat_daily_tg/health_card.py`
- `src/chat_daily_tg/cli.py`
- `tests/test_health_briefing.py`

两个实现共享 `application.py` 时，用电部分只在健康段写入完成后插入用电段；睡眠部分只增加不重发主日报的补发入口。现有无关改动全部保留。

## 验证

用电测试覆盖归零前收盘、次日 00:10 内归零、缺日、账本原子写入、七天周汇总和月汇总。睡眠测试覆盖完整昨夜记录、未同步文案、旧记录不能冒充昨夜，以及补发标记的一次性语义。

实现时运行对应的聚焦测试。实际 HA 查询和 HAE 解析已在本文的边界核实中完成；未重新发送 Telegram，也未改动 launchd。
