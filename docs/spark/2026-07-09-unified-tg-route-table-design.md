# Spec: TG fleet 统一路由表

日期：2026-07-09
状态：已确认，待实现
来源：三机 fleet 实测（Mac + r4s + bwg）+ 对比 LYiHub/labs-ArchiveAssistant 三省六部方法后的结论；自家 spark（2026-07-05 auq4me merge blindspot pass）已点名"统一的事件→通道路由模型才是核心资产"。

## 问题

全 fleet 推送目标只有两个（forum 群 `-1004424841223` 按 topic 分流 + DM `8424944105`），但路由配置散在 5 处：

1. Mac/r4s `~/qwenproxy/.tg-notify-targets.json` —— 唯一真路由表，chat-daily-tg 全家走它（topics: alert=10, chat_daily=17, twitter=19, files=23, macrumors=41, channels_news=41, bilibili=486）
2. bwg `/root/x_monitor/config.json` —— twitter/macrumors/growth 三个 thread 号平铺字段，与上表**手工冗余**（改一个 topic 要改两台机器）
3. r4s `/opt/market-recap/.env` —— chat+thread 硬编码，且 thread 17 蹭 chat_daily 的 topic，表里无 market_recap key
4. r4s `/opt/r4sbot/cc98_config.json` —— cc98 bot 内容全推 DM（daily 十大与 want-watch 命中混流，全 fleet 唯一没进 forum 的内容型任务）；health alert 自带一份 chat/thread
5. `/opt/crypto-monitor/config.json` —— 已停用

另有一个结构性缺陷：chat-daily-tg 的 `resolve_tg_target`（run_daily.py:50-67）裸 `except` 静默回落 DM，路由错误（表损坏、key 拼错）不可见。

## 决策记录

- 分发机制：**主副本 + 推送脚本**（否决 task-monitor HTTP 分发——引入运行时网络依赖，对 7 个 topic 规模过重；否决只做漂移核对——不解决手工冗余）
- market-recap：登记 `market_recap=17`，继续与 chat_daily 共用 thread，只消灭硬编码，不动推送位置
- cc98 want-watch：迁入 forum，新建独立 `cc98` topic（否决进"资讯"41——定向关键词命中会被泛资讯流淹没）
- cc98 daily 十大：与 want-watch 一起迁入 `cc98` topic；DM 只保留 poll 交互和账号级告警

## 设计

### 事实源与 schema

- `~/qwenproxy/.tg-notify-targets.json` 保持现有 schema（`chat_id` + `alert_thread_id` + `topics{key: thread_id}`），不加版本号、不加新字段。
- **Mac 副本为唯一事实源**：所有编辑（含将来 createForumTopic 后的回写）只发生在 Mac，改完跑 sync。
- 三台机器统一路径 `~/qwenproxy/.tg-notify-targets.json`（root 用户即 `/root/qwenproxy/…`；bwg 需新建 `~/qwenproxy/` 目录）。
- 新增 key：`market_recap=17`、`growth=497`、`cc98=<新建>`。cc98 thread 一次性用 Bot API createForumTopic 创建（在 Mac 执行），thread_id 登记进表。

### 同步脚本 `scripts/sync_tg_targets.sh`（本仓库，沿用 deploy.sh 先例）

流程（串行 r4s → bwg，任一步失败即停）：

1. 本地 `python3 -c json.load` 校验 JSON 合法；
2. 对每台机器：ssh 拉取远端现状 → 显示 diff → scp 推送 → ssh 回读并与本地比对，一致才算成功；
3. 结尾输出"已同步 N 台"提示。

不加常驻核对 cron：sync 自带回读验证，改表与推送是同一个动作，剩余风险只有"改了忘推"，由脚本必经性覆盖。

### 消费方改造（4 个代码库）

| 代码库 | 改动 |
|---|---|
| chat-daily-tg（本仓库） | `resolve_tg_target` 裸 except 拆细：文件缺失 / JSON 损坏 / key 缺失分别捕获；回落 DM 行为保留（"rather deliver to DM than drop"），但**回落时经 notify_failure 发告警**说明原因。notifier 的 alert 路径已有 DM 兜底，表坏时告警最终落 DM，无递归风险。 |
| x_monitor（bwg） | `twitter_monitor.py` / `macrumors_daily.py` / `growth_digest.py` 优先读 targets.json 的 `twitter`/`macrumors`/`growth`；读不到回落现有 config.json 字段 + log warning（过渡安全网，config.json 字段保留不删）。 |
| market-recap（r4s） | `recap.sh` 的 thread 硬编码改为从 targets.json 取 `market_recap`（jq 或一行 python）；`.env` 只留 token 与代理。 |
| r4sbot（r4s） | `cc98_telegram_bot.py` 的 `daily`/`want-watch` 推送目标改为 targets.json 的 `cc98` key（群 chat_id + thread）；`cc98_health_check.py` 改读 `alert` key。DM 保留 poll 交互，并作为推群失败的兜底。 |

### 验收标准

1. 三台机器 targets.json 内容一致（sync 回读通过）。
2. 改任一 thread 号只需改 Mac 一处 + 跑一次 sync，全 fleet 生效。
3. 故障注入：手工把表改坏后跑日报——内容到 DM，且收到说明回落原因的告警。
4. cc98 daily 与 want-watch 出现在群 `cc98` topic；DM 不再收到内容型消息（交互与告警除外）。
5. 其余任务推送位置全部不变（回归：bilibili=486、twitter=19、macrumors/channels_news=41、chat_daily/晨报=17、growth=497、alert=10、files=23）。

### 范围外

- crypto-monitor（休眠）、bwg-monitors（死代码）不改。
- 5 套 TG send 封装不合并（独立话题）。
- 不引入 HTTP 分发、不加表 schema 版本化、不做漂移核对 cron。

## 实现注意

- x_monitor / market-recap / r4sbot 的改动在各自机器上的代码目录进行（不在本仓库），实现时逐机串行、逐个验证（用户 SSH 串行 guardrail）。
- cc98 迁群改变了 want-watch/daily 的失败语义：推群失败回落 DM 时应带原因前缀，与 chat-daily-tg 的回落告警口径一致。
