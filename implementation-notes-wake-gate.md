# Implementation Notes — 日报 wake-gate（2026-07-17）

需求：取消 9:00/13:00 catch-up；7:05 起每 5 分钟轮询"起床内容"，起床后健康卡与群聊总结一起投递；等不到则先把群聊总结推出去。

## Design Decisions

- **起床信号 = Watch 今晨睡眠段同步到 iCloud**（`sleep_ending(wake_day)` 非空且 `end.date() >= wake_day`）。用户确认选此方案；复用现有 `wake_sleep` 语义，健康卡"起床：HH:MM"与开闸判据同源。
- **先等信号、再跑全管线**（导出→总结→推送整体后置）。用户要求"能一起发就一起发"；总结在开闸后才跑，健康卡必然带上真实起床时刻，一次投递。代价是起床后 ~5-10 分钟才送达。
- **13:00 兜底强制投递**（`--wake-deadline`，用户选定）。到点未见信号直接跑管线，健康卡回落为"今晨睡眠数据尚未同步"——即"先把群聊总结推出去"。
- **轮询放 `wait_for_wake_signal()`（health_briefing.py）**，run_daily 只在 `--wait-for-wake` 时调用且 try/except 包裹：等待逻辑任何异常（含 deadline 格式错）都降级为立即执行，不阻塞投递。
- **每次轮询新建 `HealthExportReader`**：其 run cache 会把 "file missing" 钉死，复用实例永远看不到 iCloud 落盘。

## Deviations

- **顺带删除 wrapper 的 0–15 分钟 jitter**。其注释自述 cosmetic 且存在理由是"避免与 9:00/13:00 catch-up 相撞"——catch-up 没了，且发送时刻现在跟随真实起床时间，天然有抖动。`CHAT_DAILY_NO_JITTER` 全仓无其他引用。
- **bwg task-monitor `daily` 阈值需 26h→32h（115200s），未完成**：投递时刻从固定 7:05+ 变为 7:06–13:45 区间，正常相邻两天心跳间隔可达 ~30.6h，26h 阈值健康运行也会误报。远程写操作被权限拦截，命令已交用户执行（含 `systemctl restart task-monitor`，tasks.json 注释要求）。

## Tradeoffs

- **门控要求睡眠段结束在 wake_day 当天**（只在 gate 侧加，不动 `sleep_ending` 本身）：`sleep_ending` 取窗口内最长簇，昨晚 ≥2h 傍晚小睡若先同步会在 7:05 首查误开闸。健康卡的 `wake_sleep` 语义保持原样，避免牵连既有展示逻辑。
- **launchd 单触发 + 触发合并替代 catch-up**：7:05 被睡过时 launchd 唤醒后补发（`--skip-if-done` 挡已交付日）；等待中入睡则进程冻结、唤醒后循环继续。「电池+合盖」盲区不变。
- **caffeinate -is 现在可能持锁数小时**（等待期间全程持有）。插电场景本就有 disablesleep daemon；电池+开盖+人未醒属边缘，接受耗电换"醒后 5 分钟内送达"。
- **手动补跑不带 `--wait-for-wake`**：flag 只写在 wrapper 里，`--date` 补跑旧日期立即执行（旧日期首查即中或已过 deadline，均零等待）。

## Open Questions

- bwg 阈值改完后，若某天想更早兜底（如 11:00），只改 wrapper 里 `--wake-deadline` 即可，但记得同步缩 bwg 阈值。
- 理论残余边界：Watch 半夜若把未完整的过夜睡眠段提前同步（≥2h 且结束在今天凌晨），闸门会提前开。现有 AutoSync 行为（睡眠段醒后才发布）下不出现；若观测到日报凌晨误发，再加"episode.end 距 now 不超过 N 小时"约束。
