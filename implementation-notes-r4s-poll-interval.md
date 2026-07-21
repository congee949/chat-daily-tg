# Implementation Notes — r4s B站/YouTube digest 轮询间隔 (scheme B)

2026-07-19。目标：YouTube 10–15min 随机、B站 20–30min 随机；cron 原生不支持随机间隔，用 due_gate + */5 探测。

## Design Decisions

- **scheme B（due_gate 门控）**：crontab 固定 `*/5`，`due_gate.sh check <name>` 判断是否到期；成功跑完后 `schedule` 写入下一 due epoch。失败不 schedule → gate 保持 due，下次 */5 重试。
- **due_gate 必须在 hb-wrap 之前**：`check && hb-wrap -- run`。若 hb-wrap 包住 check，skip tick 会 exit 0 假绿心跳。
- **仅成功后 schedule**：wrapper 在 `python3 run_daily.py` rc=0 时才调 schedule；失败/alert 后直接 exit rc。
- **随机用 awk srand**：OpenWrt ash 无 `$RANDOM`；区间含端点 `[min_s, max_s]`。
- **状态文件** `$CHAT_DAILY_DATA_DIR/state/due-<name>.next`（默认 `/root/chat-daily`）；缺文件/损坏视为 due（自愈）。
- **task-monitor 阈值**：bilibili 4500s（≈max 30min×2.5）；youtube 2700s（≈max 15min×3）；cadence 文案改为「N-Mmin随机」。

## Deviations

- 无。与用户指定 scheme B 一致。

## Tradeoffs

- 实际间隔 = 随机目标 + 最多 ~5min 探测粒度（可接受，换 cron 简单与 fail-retry 免费）。
- flock skip 仍 exit 0（沿用旧语义）：前一轮在跑时本轮跳过不 schedule，gate 仍 due，下一 tick 再试——正确。

## Open Questions

- 无。
