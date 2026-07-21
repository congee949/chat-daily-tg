# Implementation Notes — 日报 wake-gate（2026-07-17，2026-07-21 修订）

需求（初版）：取消 9:00/13:00 catch-up；7:05 起等起床信号，起床后健康卡与群聊总结一起投递；等不到则先把群聊总结推出去。

**2026-07-21 修订**：用户确认「没有睡眠数据就先发总结」。Health Auto Export / iCloud 滞后或 `Resource deadlock avoided` 解码失败时，原 5 分钟轮询到 13:00 会把整份群聊日报拖到中午。策略改为 **单次探测、缺数据立刻投递**。

## Design Decisions

- **起床信号 = Watch 今晨睡眠段同步到 iCloud**（`sleep_ending(wake_day)` 非空且 `end.date() >= wake_day`）。复用 `wake_sleep` 语义；有数据时健康卡带真实起床时刻。
- **无睡眠数据 → 立刻跑全管线**（2026-07-21）。睡眠是可选 enrichment，不是门闩。单次 probe 后 `False` 即开跑，不再 spin 到 `--wake-deadline`。
- **`--wake-deadline` / `poll_seconds` 保留签名与 launchd 环境变量兼容**，逻辑层不再等待。
- **`wait_for_wake_signal()` 仍 try/except 包裹**：reader 崩溃降级为立即执行。
- **每次 probe 新建 `HealthExportReader`**：保留原 shape，避免 cache 钉死 file-missing（若将来再加短等待）。

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
