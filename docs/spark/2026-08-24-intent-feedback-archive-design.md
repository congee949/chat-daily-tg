# 读完反馈与意图归档 MVP（2026-08-24）

## 目标

把用户对已投递内容的“读完/展开/切换/过滤”反馈记录成可重放的追加事件，并在跨任务稳定的本地工作目录中按大 topic 和处理意图分流。这个 MVP 只提供纯 Python API 和 `python -m chat_daily_tg.intent_feedback` CLI；它不读取 Telegram、不启动第二个 `getUpdates` 消费者、不调用远端模型，也不修改 R4S `media_sent_ledger` 或 Mac `sent_content_ledger`。

实现位置：`src/chat_daily_tg/intent_feedback.py`；测试：`tests/test_intent_feedback.py`。

## 工作目录

默认根目录是 `~/chat-daily/intent-feedback/`。测试和 dry-run 应显式传入临时 `root`，避免触碰运行数据。每次成功记录会追加两份相同事件：

```text
<root>/
  events.jsonl                         # 全局事件日志，唯一事实源
  topics/<topic-slug>/<output-mode>/
    events.jsonl                       # 可直接交给对应工作队列的派生视图
  topic_reclassifications.jsonl        # 大 Topic 修正的追加式审计日志
```

`topic-slug` 由 NFKC、大小写折叠和路径安全字符归一化得到；空或不可用 topic 统一为 `unclassified`。`output-mode` 为 `read`、`expand`、`switch`、`filter` 之一。映射未知不会伪造 chat/message ID，仍保留在相同 topic/intent 目录，并在事件的 `source.mapping_status` 中明确标记为 `unknown`。

`events.jsonl` 始终是不可改写的事实日志。分类器发现误判时，`reclassify-topic` 会先向 `topic_reclassifications.jsonl` 追加旧/新 Topic、原因与时间，再原子重建相关 `topics/` 派生工作视图；因此历史事实与修正理由都保留，而活跃工作目录不会继续夹带已知误分类。

## 事件 schema

每行是 `intent-feedback.v1`：

```json
{
  "schema": "intent-feedback.v1",
  "event_id": "button:telegram-target:91:expand",
  "idempotency_key": "button:telegram-target:91:expand",
  "event_type": "expand",
  "intent": "expand",
  "output_mode": "expand",
  "output_intensity": "expand",
  "occurred_at": "2026-08-24T01:00:00+00:00",
  "content_id": "telegram:-100123:13545",
  "topic": {"label": "AI 工具", "slug": "ai-工具", "method": "explicit", "confidence": null},
  "source": {
    "mapping_status": "confirmed",
    "chat_id": -100123,
    "message_id": 13545,
    "thread_id": 41,
    "kind": "telegram",
    "url": "https://t.me/example/13545"
  },
  "target": {"mapping_status": "confirmed", "chat_id": -100456, "message_id": 91},
  "content": {
    "content_id": "telegram:-100123:13545",
    "title": "标题",
    "text": "投递给用户的原始卡片内容",
    "content_hash": "sha256..."
  },
  "metadata": {"producer": "hermes"}
}
```

`source` 是内容来源消息，`target` 是用户实际反馈所对应的投递消息；两者都要求显式的 `chat_id + message_id` 才能为 `confirmed`。部分或缺失映射只写 `{ "mapping_status": "unknown" }`，不会从 `content_id`、URL 路径或另一条消息推导 ID。

## 幂等与故障恢复

- 调用方可传 `idempotency_key`。相同 key 且语义字段一致时重复调用不产生新行。
- 未传 key 时由内容 ID、event type、output mode、topic、映射、内容 hash 和 metadata 计算 `auto-<sha256前缀>`，适合重试；若需要同一内容的第二次独立阅读，应传新的 key。
- 同 key 但语义字段不同抛出 `IdempotencyConflictError`，防止静默改写用户意图。
- 事件中心日志和 topic 视图都按追加写入；若进程在两次追加之间中断，下一次相同调用会补齐缺失的一侧而不复制另一侧。
- 同一根目录用权限为 `0600` 的 `.archive.lock` 做跨进程互斥；Hermes、CLI 和其他 Codex 任务并发记录时，共享同一套幂等检查与双追加临界区。
- 所有目录创建仅限传入的 feedback root。实现没有任何对 `media_sent_ledger.jsonl` 或 `sent_content_ledger.jsonl` 的写入路径。

## 三种处理力度

`expand`、`switch`、`filter` 是后续工作队列的三种意图：

| output mode | 后续动作 | 默认回答力度 |
| --- | --- | --- |
| `expand` | 保留在当前大 topic，补充背景、关联和必要细节 | 详细展开 |
| `switch` | 降低当前内容优先级，切换到同 topic 的其他信息或新 topic | 简短确认切换 |
| `filter` | 标记为噪声/侵扰，避免要求用户精读 | 极短回复或不打扰 |

`read` 只表示已读反馈，不强行推断价值；它可以与显式 `output_mode` 搭配，作为后续意图模型的训练样本。反馈本身不等于系统替用户判断“值得查看”。

## 本地分类与 embedding seam

`TopicClassifier`、`Embedder`、`Reranker` 是 `typing.Protocol`，调用方可注入本地实现。MVP 不添加重量依赖，也不发远端请求。`default_topic_classifier()` 先按“AI 与自动化 / 科技与产品 / 金融与市场 / 健康与运动 / 学习与研究 / 商业与社会 / 文化与内容”七个大类落目录；匹配不到时进入 `unclassified`。`KeywordTopicClassifier` 和 `hashed_token_features` 只是低成本基线，不是最终语义模型。后续接本地 embedding 时，应把模型版本、维度和特征哈希写入 `metadata`，并保持事件 schema 向后兼容。

## 适配 Hermes 的建议边界

1. Hermes 继续负责唯一 Telegram update/reaction 消费和现有 ledger 映射；解析出已确认的 source/target 后调用 `FeedbackStore.record`。
2. 映射未找到时照样记录反馈，但传入 `source=None` 或部分映射，让事件显式进入 `unknown` 状态；不得为了归档补猜 ID。
3. 发送摘要/展开内容仍遵守现有 write-after-send、seen 和降级规则。feedback archive 是意图输入，不是投递成功证明。
4. 先通过本地 CLI/API 回放 JSONL 验证分类和幂等，再由上层决定是否向 Telegram 发送简短回复或展开内容。

## Podcast4Bot 四按钮适配

Podcast4Bot 的快筛卡已使用现有 `callback_query` 更新流接入四个意图按钮，不新增 `getUpdates` 消费者：

- `展开`：记录 `expand`，将精读稿作为独立消息生成并保留快筛卡；完成后按钮保持“已展开”，若用户此前已点“读完”，同时保持“已读”。
- `读完`：仅记录 `read` 并更新按钮状态，不自动推断价值，也不启动精读。
- `切换`：记录 `switch`，删除分析卡并清除后续新问答所使用的当前内容指针；用户原始的 Telegram 内嵌来源消息保留。
- `过滤`：记录 `filter`，删除分析卡并清除后续新问答所使用的当前内容指针；转写缓存和原始来源消息保留。

四种新反馈仅允许配置中的 owner 触发。归档写入失败时只记告警，不阻断按钮原有动作；幂等键由目标消息、owner 和反馈模式组成，同一按钮的 Telegram 重投不会重复追加事件。MVP 不追溯取消点击前已进入串行队列的 ask；若后续要把“过滤”定义为严格取消所有在途回复，需要另加 ask job 的状态门禁和取消回执，不能只依赖当前内容指针。

用户本人发送且 Telegram 已生成内嵌卡片的 YouTube 链接采用更窄的快筛头部：最终卡作为原消息的 reply，只显示纯文本 `⏱ 时长`，不重复标题、频道、URL 预览或 `📌 来源`。订阅来源仍保留 `📌 原 Topic`。B站与抖音是否采用同一规则等待各自真实卡片 canary 后决定，不从 YouTube 行为外推。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_intent_feedback.py
PYTHONPATH=src .venv/bin/python -m chat_daily_tg.intent_feedback record read \
  --root /tmp/intent-feedback-smoke --content-id demo-1 --topic reading --text hello
```

该验证只触碰临时 root，不代表 Hermes、Telegram 或生产 ledger 已部署。
