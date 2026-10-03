# YouTube 官方频道精选验证：2026-10-01

OpenAI 与 Claude 已接入 r4s 的 YouTube digest，频道策略为 `ai_official`。
r4s 的共用文本、视觉摘要模型改为经 CPA 调用 `gpt-6.1-sol`。原有十三个订阅、
时长阈值、回看窗口、cron 与话题路由保持原配置。

有效用法见 [官方频道指南](../youtube-selection.md)。本记录保存该版本的实测结果。

## 模型与执行位置

Mac 通过 SSH 别名 `r4s` 调用 FriendlyWrt，用户为 `root`，
代码目录为 `/root/chat-daily-tg`，配置与状态位于 `/root/chat-daily`。
本次模型请求从 r4s 发出。

CPA 位于 BWG。运行中的 `cliproxy-cpa` 容器使用
`/srv/cliproxy/config/config.yaml`；Caddy 在 Tailscale 地址的 8317 端口接入请求。
r4s 实测调用端点为 `http://100.87.113.14:8317/v1`，
认证变量为目标机器 `.env` 的 `CLIPROXY_API_KEY`。

| 验证 | 结果 | 实测耗时 |
|---|---|---|
| r4s 带认证读取 CPA `/v1/models` | HTTP 200，目录包含 `gpt-6.1-sol` | 未单独计时 |
| 文本 JSON 探针 | HTTP 200，返回模型为 `gpt-6.1-sol`，结构与预期相同 | 3.926 秒 |
| 图片识别，200-token 输出上限、xhigh | HTTP 200，正确识别左侧红色方块和右侧蓝色圆形，正常结束 | 5.844 秒 |
| 真实频道样本精选，medium | HTTP 200，返回结构通过代码校验 | 48.162 秒 |
| 正式 guard 入口的实际精选调用 | 成功，模型 `gpt-6.1-sol`，一次尝试 | 12.894 秒 |

原始探针结果：[文本](youtube-selection-2026-10-01/cpa-text-probe.log)、
[图片](youtube-selection-2026-10-01/cpa-image-probe.log)。

## 频道样本

通过 r4s 的 YouTube Data API 解析两个 handle，并取得每个频道十五条视频的标题、
简介、发布时间和时长。原始数据保存在
[channel-samples.json](youtube-selection-2026-10-01/channel-samples.json)。

| 阶段 | 数量 |
|---|---|
| 原始视频 | 30 |
| 现有短视频规则过滤 | 20 |
| 精选输入 | 10 |
| 精选保留 | 8 |
| 精选过滤 | 2 |

过滤的两条为 Standard Chartered 企业案例与 Sophos 合作伙伴生态访谈。
标题和简介缺少具体操作或技术细节，触发 `business_story_without_detail`。
完整 DevDay keynote、Codex Cloud 配置教程、Claude Code 验证循环教程和 Stripe
技术访谈被保留。低置信度的 Ramp、Notion 样本继续保留，未触发终态抑制。

[预览结果](youtube-selection-2026-10-01/selection-preview.json)与
[调用和筛选日志](youtube-selection-2026-10-01/selection-preview.log)保存完整结果。
预览前后，生产 `youtube_seen.txt` 与 `dedup_journal.jsonl` 的 SHA-256 相同。

## 正式投递

2026-10-01 23:08（北京时间），通过
`/bin/sh /root/chat-daily-tg/scripts/run_youtube_r4s.sh` 执行正式入口：
十条未见候选中八条被现有短视频规则过滤，剩余两条经 CPA 精选后成功投递。

| 视频 | Telegram 消息 ID | 话题 ID | ledger 内容 ID |
|---|---|---|---|
| Meet the all new Codex Cloud | 10864 | 2009 | `youtube:7Bv68f5szSU` |
| OpenAI DevDay 2026 Keynote (FULL) | 10865 | 2009 | `youtube:Fls_onRviPM` |

两条消息均通过 tgcli 同步后回读，内容包含中文“视频看点”与对应视频按钮。
两条 seen 已落盘；`media_sent_ledger.jsonl` 分别记录对应消息 ID、话题和视频 URL。
正式入口退出码为 0，日志记录 `2/2 cards sent (no_push=False)`。
Claude 频道在该轮回看窗口内没有通过短视频规则的未见候选，尚无新增投递回执。

## 去重与失败记录

X 账本读取发现两条损坏 JSON 行，1675 条已确认记录的正文哈希通过校验。
损坏行被逐条隔离，原始账本保持原样。同一 YouTube 视频链接的跨 X 抑制已通过隔离测试；
本次两张正式卡片未命中该规则。相同主题和没有相同视频链接的 X 消息继续保留。

接入前，原 r4s 摘要路线调用 VibeKey 返回 HTTP 404；
原 DeepSeek 路线返回 HTTP 401。原始失败日志分别见
[摘要路线](youtube-selection-2026-10-01/initial-summary-route-failure.log)和
[DeepSeek 路线](youtube-selection-2026-10-01/initial-deepseek-route-failure.log)。
最终部署使用 CPA，没有新增 VibeKey 或 DeepSeek 模型配置。

## 验证与回退

本地相关测试通过：113 项，覆盖配置、筛选、YouTube 抓取、卡片投递和入口。
包含非法模型输出、低置信度、相同主题保留、同视频链接抑制、坏账本行、
审计写入失败、预览状态隔离、数量截断顺序及 write-after-send。
两个 r4s wrapper 均通过 `sh -n`。

部署先取得 YouTube 与 B站两把运行锁，再校验七个受影响源码与 wrapper 的原始哈希，
仅应用本次差异。部署后再次读取全部文件，七项 SHA-256 与部署清单一致。
本地与远端保留各自原有源码差异；配置修改保留注释与其他条目。

备份位于 `r4s:/root/chat-daily/backups/youtube-selection-20261001/`，
包含原配置、受影响文件和部署清单；不含密钥与运行状态。
回退时先取得相同两把锁，核对当前哈希与部署清单，再恢复对应文件。
保留已投递的 seen 和 ledger，避免重复发送。
恢复原模型配置会同时恢复上述失败路线。

没有提交、推送仓库，也没有重载服务或修改 cron。
