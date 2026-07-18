# X 官方开发与额度消息推送设计

日期：2026-07-18
范围：BWG `/root/x_monitor` 的即时 Telegram 推送策略；本设计不增加 Status、changelog 或日报订阅。

## 1. 目标

只主动推送两类真正需要打断用户的信息：

1. Claude Code / Codex / API / SDK / 模型等可立即使用的开发更新。
2. 套餐权益、weekly/5-hour limits、credits、reset、退款与额度异常补偿。

非目标：公司研究、客户案例、活动、教程、周边、普通产品宣传和社区转推。它们不进入本即时通道。

## 2. 账号策略

| 账号 | 来源角色 | 原创门 | 允许即时事件 |
|---|---|---|---|
| `ClaudeDevs` | Claude 官方开发者账号 | 仅原创 | `dev_release`、`model_api`、`quota_reset`、`quota_compensation`、`quota_policy`（2026-07-19 补） |
| `OpenAIDevs` | OpenAI 官方开发者账号 | 仅原创 | `dev_release`、`model_api` |
| `claudeai` | Claude 产品官号 | 仅原创 | `plan_entitlement`、`quota_policy`、`model_access` |
| `OpenAI` | OpenAI 公司官号 | 仅原创 | `model_launch`、`major_product_launch`、`permanent_plan_change` |
| `thsottiaux` | OpenAI Codex / ChatGPT 团队个人账号 | 仅原创 | `quota_reset`、`quota_policy`、`credit_grant`、`quota_compensation` |

`AnthropicAI`、`ChatGPTapp` 不进入这条即时策略。

配置文件只引用稳定的策略名，不把大量关键词复制到账号条目中：

```json
[
  {"username": "ClaudeDevs", "enabled": true, "push_policy": "claude_dev_original"},
  {"username": "OpenAIDevs", "enabled": true, "push_policy": "openai_dev_original"},
  {"username": "claudeai", "enabled": true, "push_policy": "claude_entitlement_original"},
  {"username": "OpenAI", "enabled": true, "push_policy": "openai_major_original"},
  {"username": "thsottiaux", "enabled": true, "push_policy": "codex_quota_original"}
]
```

策略定义留在代码中的集中 registry，便于测试、版本化和审查。未知策略必须启动失败，不能静默退化为全量推送。

实施时还应把每个账号当前的 X immutable user ID 写入配置。抓取结果的 user ID 与预期不一致时停止该账号并告警，避免 handle 更名或被重新注册后把陌生账号当成官方来源。用户名比较统一用 case-insensitive canonical form。

## 3. 判定流水线

```text
fetch
  -> seen check
  -> originality hard gate
  -> account policy classification
  -> event facts extraction
  -> same-thread bundling
  -> cross-account event dedup
  -> Telegram render/send
  -> seen + pushed event checkpoint
```

### 3.1 原创硬门

以下均判为纯转推并禁止即时推送：

- `retweeted_status.id` 存在；
- 规范化字段 `is_retweet=true`；
- 数据源降级导致结构字段缺失，但正文以 `RT @` 开头。

Quote tweet 不是自动放行：只有账号自己的评论具有实质内容，并且评论本身或评论加被引上下文能通过该账号事件策略时才放行。只写 “this”“great” 或一个 emoji 的引用不推。

原创判定信息缺失或互相冲突时 fail-closed：记录 `policy_skip: originality_ambiguous`，不即时推送。

### 3.2 事件分类

事件枚举：

```text
dev_release
model_api
model_launch
major_product_launch
plan_entitlement
permanent_plan_change
model_access
quota_policy
quota_reset
credit_grant
quota_compensation
irrelevant
ambiguous
```

判定采用两层：

1. 高置信 code rules：账号角色、完成态动词、产品实体、套餐/额度实体和否定词共同判定。
2. 只有官方原创且 code rules 返回 `ambiguous` 时，调用结构化分类器；输出必须包含 `event_type`、`confidence`、原文 `evidence_spans` 和结构化 facts。证据不能在原文定位、JSON 不合法或分类器不可用时，不推送。

分类器只能在账号策略允许的事件集合内选择，不能把 `OpenAI` 的研究宣传升级为开发更新。

原文始终作为不可信数据传给分类器：分类调用不带工具权限，不执行推文中的指令；`evidence_spans` 必须逐字存在于规范化原文中。涉及区域、套餐或时间的限制必须进入 facts，renderer 不得把 “US paid users” 泛化成“所有付费用户”。`permanent_plan_change` 也只在原文明示长期/正式变更时成立。

### 3.3 `thsottiaux` 强过滤

高置信放行必须同时具备：

- 作用对象：`Codex`、`ChatGPT Work`、`paid users`、明确套餐或 “everyone/all users”；
- 已发生或确定将发生的动作：reset、banked reset、limit removal/change、credit grant、refund/compensation；
- 至少一个结构化结果：范围、额度、时间、套餐、百分比或补偿方式。

否定、征询和玩笑优先于正关键词：

| 原文模式 | 结果 |
|---|---|
| `We've reset usage limits ...` | `quota_reset`，推送 |
| `Added a banked reset ...` | `credit_grant`，推送 |
| `Should we reset ...?` | 征询，不推 |
| `Thinking I am about to announce a reset. But no.` | 明确否定，不推 |
| `If this gets blocked, I owe you a reset.` | 条件玩笑，不推 |

该策略不使用点赞、浏览量或转推量门槛。额度变化在刚发布且互动为零时仍具有即时价值。

### 3.4 Claude 职责边界

- `ClaudeDevs`：已经执行的 5-hour/weekly reset、技术故障、多扣费、退款和补偿；**以及额度政策类权益公告（`quota_policy`）**。
- `claudeai`：套餐未来包含多少、模型访问期、weekly allocation、credits 和 Pro/Max/Team 权益。

> **2026-07-19 修订**：原设计假设权益/额度政策公告只发在 `claudeai`，实测 Anthropic 把
> "weekly limits 50% higher through Aug 19" 发在了 `ClaudeDevs`，被 `policy:no_developer_event`
> 过滤漏推。修复：两个 Claude 官号共用权益识别（`_entitlement_event`），`ClaudeDevs` 只放行
> 其中的 `quota_policy`；`model_access` / `plan_entitlement` 仍归 `claudeai`，营销性 credits
> （hackathon/startup program 等）在两号都排除。

`ClaudeDevs` 对 `claudeai` 的纯转推会先被原创硬门拦截；不需要额外维护账号名单式 RT 去重。

## 4. 事件事实模型

分类通过后生成 `PushEvent`，renderer 不再从自由文本二次猜测：

```json
{
  "event_id": "quota_reset:codex:2026-07-18:paid",
  "event_type": "quota_reset",
  "source_account": "thsottiaux",
  "source_role": "openai_team",
  "product": ["Codex", "ChatGPT Work"],
  "audience": ["paid users"],
  "change": {"weekly_usage": "reset"},
  "effective_at": null,
  "action": null,
  "evidence_spans": ["reset usage limits for all paid users"],
  "tweet_ids": ["2078320950488297917"],
  "conversation_id": "2078320950488297917",
  "confidence": "high"
}
```

没有原文依据的字段保持 `null`，消息中不渲染，禁止推断诸如“已经到账”“需要重启”之类的动作。

## 5. 合并与去重

### 5.1 同账号 thread

- `quota_reset`、`quota_compensation`：首条立即发；同作者 15 分钟内的同类自回复只在范围、金额、时间或操作发生实质变化时发“更新”。
- `dev_release`、`model_api`、`model_launch`：等待 90 秒 idle window，按 `conversation_id` 合并自回复；最长等待 3 分钟。
- 不抓其他人的回复树。

### 5.2 跨账号事件

新增事件级索引，不能复用仅按 tweet ID 的 `.pushed_index.json`：

```text
event key = normalized product + event_type + material facts fingerprint
            + source event reference / announcement time
TTL       = 24h（普通发布）/ 72h（套餐与额度；仅为索引保留期）
```

TTL 不等于重复判定窗口：

- `quota_reset` 只在 15 分钟内、事实指纹一致且没有 `again` / `another` / 新生效动作时视为同一事件；同一天多次真实 reset 必须全部推送。
- `quota_compensation` / `credit_grant` 在金额、适用人群、领取方式或批次变化时产生新事件。
- 套餐和模型访问变化使用 72 小时窗口；模型发布使用 24 小时窗口。
- 能引用同一 source tweet ID 时优先用明确引用关系，不依赖模糊文本相似度。

规则：

- `claudeai` 原创套餐信息已推后，`ClaudeDevs` 纯 RT 已由原创门拦截。
- `thsottiaux` reset 已推后，`OpenAI` 的纯 RT 已由原创门拦截。
- `OpenAI` 模型首发与 `OpenAIDevs` API 细节不是同质重复：前者发“发布”，后者只有新增 model id、API endpoint、价格、限制或迁移信息时发“技术补充”。
- 同一事件第二条没有新增结构化 facts 时记录 `policy_skip: no_new_facts`。

去重只能在 Telegram 明确送达后登记；ambiguous delivery 沿用现有“投递优先、允许极少重复”的语义。

## 6. Telegram 消息设计

所有消息控制在一屏内，先写变化，再写来源。原文长 thread 通过按钮打开。

### 6.1 额度重置/补偿

```text
⚡ Codex 额度已重置

范围：所有付费用户
变化：Codex 与 ChatGPT Work usage limits 已重置
来源：Tibo · OpenAI Codex/ChatGPT 团队

[查看原文]
```

如有后续实质更新：

```text
↻ Codex 额度更新

新增：本次也可能重置其他 rate limits
状态：来源尚未确认具体范围
来源：Tibo · OpenAI Codex/ChatGPT 团队

[查看更新]
```

个人团队账号不得显示成“OpenAI 官方公告”；使用“OpenAI Codex/ChatGPT 团队”标签并保留原作者。

### 6.2 套餐与 weekly limits

```text
💳 Claude 套餐与额度变化

Max / Team Premium：Fable 5 纳入套餐，使用上限为 50%
Pro / Team Standard：通过 usage credits 使用，并发放一次性 $100 credit
生效：7 月 20 日
来源：Claude 官方

[查看原文]
```

### 6.3 开发更新

```text
🛠 Claude Code 开发更新

/code-review 新增 effort levels；ultra 会并行运行多个 reviewer agents。
可用范围：原文明确的套餐/版本（如有）
来源：ClaudeDevs · 官方开发者账号

[查看原文]
```

### 6.4 模型发布与技术补充

```text
🚀 OpenAI 发布 GPT-5.6

范围：ChatGPT、Codex、API
状态：开始逐步上线
来源：OpenAI

[查看发布]
```

```text
🧩 GPT-5.6 技术补充

API：Responses / Chat Completions / Batch
新增：Programmatic Tool Calling、Multi-agent beta
来源：OpenAIDevs · 官方开发者账号

[查看技术原文]
```

## 7. 可观测性与状态

每条新推文必须留下一个可检索决策：

```text
account, tweet_id, originality, policy, event_type,
decision(push|skip|update), reason, matched_rules,
classifier_backend, confidence, event_key, telegram_message_ids
```

核心计数：

- `policy_push_total{account,event_type}`
- `policy_skip_total{account,reason}`
- `policy_ambiguous_total{account}`
- `event_dedup_total{relation}`
- `classifier_failure_total{backend}`

过滤仍进入 seen，避免每轮重复判断；发送失败继续进入现有 `push_retry`，不能因超过 45 分钟窗口而永久丢失。

新增账号首次启用采用 seed-only，不补推整段历史。正常运行中按事件类型覆盖现有统一 45 分钟 freshness：

- `quota_reset` / `quota_compensation` / `credit_grant`：6 小时；
- `dev_release` / `model_api` / `model_launch`：6 小时；
- 套餐、权益、定价和模型访问：24 小时。

这样监控短暂中断后仍能补回有行动价值的变化，同时不会恢复后灌入旧消息。若数据源没有 `conversation_id`，thread bundling 降级为 `author + self-reply reference`；两者都缺失时不猜 thread，只依赖短时事件去重。

## 8. 验收用例

至少覆盖：

1. 五个账号的纯 RT 全部不推，包括内容本身很重要的 RT。
2. `ClaudeDevs` reset 原创推送；`claudeai` 套餐原创推送。
3. `claudeai` hackathon 的 `$100k credits` 不得误判为用户 credits。
4. `thsottiaux` 实际 reset、banked reset、credits 推送。
5. `Should we reset?`、`But no`、`I owe you a reset` 不推。
6. `OpenAIDevs` 社区 showcase、Office Hours、周边不推。
7. `OpenAI` 客户案例、播客、研究宣传和 merch 不推。
8. 同一模型发布的 OpenAI 首发先推；OpenAIDevs 有新增 API facts 时推技术补充，无新增时抑制。
9. 同一天连续两次真实 reset 都推；15 分钟内无新事实的自回复不重复推。
10. classifier 超时、垃圾 JSON、未知事件 enum、伪造 evidence span 均 fail-closed，不变成全量推送。
11. 地区/套餐限定不会在消息中被泛化；缺字段保持未知。
12. Telegram 失败不登记 event delivered；下一轮绕过时间窗重试。
13. 监控中断 2 小时后，额度和模型发布仍补推；超过各自 freshness 后只记 seen。
14. （2026-07-19 补）`ClaudeDevs` 额度政策原创（如 "weekly limits 50% higher through Aug 19"）按 `quota_policy` 推送；非额度权益（model access / plan 归属）与营销性 credits 仍不从 dev 号放行。

## 9. 落地顺序

1. 扩展账号配置 `push_policy`，加入 `claudeai`、`OpenAI`、`thsottiaux`；保持官号处理顺序在前。
2. 增加原创硬门和策略 registry，以真实推文建立 fixture 回放测试。
3. 增加 `PushEvent`、额度/套餐 renderer 和 `thsottiaux` 否定用例。
4. 增加 event index 与 OpenAI → OpenAIDevs 技术补充判定。
5. 最后增加 thread idle bundling；在此之前由事件索引抑制同类自回复风暴。

远端 `/root/x_monitor` 当前存在用户未提交改动，实施时必须先隔离或基于其最新状态继续，不能覆盖。
