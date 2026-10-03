# YouTube 官方 AI 频道精选

OpenAI 和 Claude 频道使用 `ai_official` 策略：保留有具体步骤的教程、完整演示、
相关技术访谈与讨论；完整发布会需要包含实际演示或技术问答。过滤宣传片、预告、
精彩剪辑，以及缺少实质细节的客户背书。

筛选依据是标题和简介。卡片中的“视频看点”概括这些元数据，不使用未读取的视频正文。
相同主题可以包含新的操作方法、技术细节或问答，因此主题相同不触发跨平台抑制。

## 配置

在执行 YouTube digest 的机器上修改 `~/chat-daily/config.yaml`：

```yaml
sources:
  youtube:
    enabled: true
    fetch:
      whitelist:
        # 合并到现有列表，保留其他订阅。
        - channel_id: UCXZCJLdBC09xxGZ6gcdrc6A
          name: OpenAI
          selection: ai_official
        - channel_id: UCV03SRZXJEz-hchIAogeJOg
          name: Claude
          selection: ai_official
```

频道匹配使用不可变的 `channel_id`。`name` 是备注。
[`YoutubeChannel`](../src/chat_daily_tg/config.py) 定义 `selection`；
默认 `all` 保留现有订阅行为。时长规则继续由
[`YoutubeFetch.min_duration_seconds`](../src/chat_daily_tg/config.py) 控制：
已知时长不超过配置值的视频先过滤，未知时长沿用标题中的 `#shorts` 识别。

默认筛选模型为 `models.summary`。需要独立选择已配置的顶层模型别名时，设置
`sources.youtube.fetch.selection_model_alias`，别名解析使用
[`Config.resolve_model_alias()`](../src/chat_daily_tg/config.py)。
在目标机器核对模型目录并完成真实请求后，再启用该路线；凭据留在该机器的 `.env`。

r4s 通过 BWG 的 Tailscale 地址调用 CPA。模型配置示例：

```yaml
models:
  summary:
    endpoint: http://100.87.113.14:8317/v1
    model: gpt-6.1-sol
    api_key_env: CLIPROXY_API_KEY
    max_tokens: 16000
    timeout: 120
    extra_body:
      reasoning_effort: medium
```

Mac 的本地转发地址与 r4s 的调用地址分别配置。两个 r4s digest wrapper 的
`NO_PROXY` 包含 CPA 地址；YouTube、Telegram 和外部封面继续使用原有 HTTP 代理。
该 `models.summary` 与 `models.vision` 是 B站和 YouTube 共用的摘要路线，
调整后两条 digest 都会使用新模型。

## 筛选与状态

[`select_videos()`](../src/chat_daily_tg/youtube_selection.py) 在
[`fetch_new_videos()`](../src/chat_daily_tg/youtube_fetcher.py) 的数量截断前运行，
避免被过滤的视频占用 `max_per_digest`。

模型结果通过 [`parse_decisions()`](../src/chat_daily_tg/youtube_selection.py) 校验：
类别和视频 ID 必须有效，布尔值与置信度必须符合类型和范围，证据必须是标题或简介中的
连续原文。高置信度且缺少实质内容或与关注范围无关的结果才会过滤。
证据不足、未知类别、低置信度、模型异常或输出无效时保留视频。
显式预告、剪辑，以及简介中没有操作或技术细节的发布标题、客户故事和合作伙伴宣传，
使用确定性规则过滤。

过滤结果先通过 [`dedup_journal.record()`](../src/chat_daily_tg/dedup_journal.py)
写入 `~/chat-daily/state/dedup_journal.jsonl`，包含元数据、规则版本、原因和证据，
随后更新 YouTube seen。审计写入失败时保留视频。普通保留项仍在真实发送成功后写 seen。

## 与 X 的重复

[`confirmed_x_videos()`](../src/chat_daily_tg/youtube_selection.py) 读取执行机器上的
`~/chat-daily/state/x_monitor_sent_content_ledger.jsonl`。仅使用最近七天、已确认投递、
具有 Telegram 消息 ID 且正文哈希匹配的记录；副本修改时间超过一天时停用该次检查。
损坏行单独忽略并记录数量，原文件保持不变。

当已投递正文包含同一个 YouTube 视频链接时，视频卡片被过滤，审计记录保留对应的
Telegram 回执。匹配支持 watch、youtu.be、live、shorts 和 embed URL。
同主题的新视频继续参与内容筛选。X 正文没有视频链接时，这项规则无法识别同一片段；
仅凭模型名称、发布时间或话题相同进行抑制会遗漏新增内容。

## 预览与复核

通过现有入口预览：

```bash
env -u ALL_PROXY -u all_proxy python run_daily.py --youtube-only --no-push
```

在 r4s 上使用与 [`run_youtube_r4s.sh`](../scripts/run_youtube_r4s.sh) 相同的 HTTP 代理、
`PYTHONPATH` 和 `TZ=CST-8` 环境。筛选预览会调用模型、写日志和常规归档，
不会发送正文，也不会因筛选更新 seen 或审计日志。

日志中的 `YouTube selection` 包含保留或过滤结果与原因。复核过滤记录：

```bash
python3 - <<'PY'
import json
from pathlib import Path
path = Path.home() / "chat-daily/state/dedup_journal.jsonl"
if path.exists():
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("layer") == "youtube-selection":
            print(row["content_id"], row["reason"], row.get("evidence"))
PY
```

投递后核对 Telegram 卡片、`youtube_seen.txt` 与
`state/media_sent_ledger.jsonl`。调度和日志位置见 [运维指南](runbook.md)。
