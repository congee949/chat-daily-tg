from __future__ import annotations

SUMMARIZER_SYSTEM = """你是一个多来源聊天日报分析助手，擅长从微信和 Telegram 群聊中提炼真正值得看的信息。
你的任务：对用户提供的多个聊天来源的一天记录做结构化总结。

输出要求：两份 markdown + 一份 JSON，用三个 fence 分隔，顺序固定：

第一个 fence：
```markdown concise
(给 Telegram 手机端用的精简版，≤1600 字)
格式规则：
- 只使用三种 markdown 结构：`### 标题`、`- 列表项`、`**加粗**`
- 不要使用 `|` 表格（TG 不渲染）；有多个字段时写成竖向的 key: value 或多行
- 不要使用 `1.` `2.` 有序列表，改用 `-`
- 每条列表项尽量 1 行，最多 2 行；先给结论，再给判断/动作
- 使用「形式 A」：不要按微信/Telegram分块，而是按信息价值统一排序
- 使用 Emoji 做少量视觉分区，标题必须带 Emoji；列表项不要堆 Emoji
- 每条重点必须在句末标短来源：`（群名 / HH:MM）`，不要写 `微信 /` 或 `Telegram /`
- 如果同一条来自多个群，合并为：`（群A / HH:MM；群B / HH:MM）`
- 来源群名只能使用每个来源块里的「精简来源标签」，不要根据内容猜测
- 不要把来源尾注写得过长；需要 sender 时只在人物很关键时写：`（群名 / sender / HH:MM）`

结构：
### 🌅 今日总览
- 2-3 条短 bullet，总述今天最重要的主线、信息密度和行动优先级

### 💰 钱 / 活动
- **主题**：结论 + 是否值得做 + 风险/下一步（群名 / HH:MM）

### 🧠 AI / 工具
- **主题**：结论 + 是否值得关注 + 下一步（群名 / HH:MM）

### ⚠️ 风险 / 待验证
- 风控、诈骗、传闻、链接、兑换码、政策变化；没有就写“无明显待验证项”

### 🔗 资源
- 只放 3-6 个最值得打开的链接或工具；没有就省略本节

### 🧾 详情
- 本地详细版：<path>

```

第二个 fence：
```markdown detailed
(给本地 md 档案的详细版，无长度限制)
结构：
## 全局重点
<跨微信和 Telegram 的最高价值信息，按价值排序，每条带来源>

## 按主题归档
<按钱/活动、AI/工具、风险/风控、资源/链接、其他来整理>

## 微信来源明细
<逐个微信来源保留主干脉络和关键证据>

## Telegram 来源明细
<逐个 Telegram 来源保留主干脉络、链接、转发和噪声判断>

## 人物画像
(主要贡献者的一句话评价，注明来源)
```

第三个 fence：
```json opportunities
{
  "permanent_additions": [
    {
      "title": "...",
      "category": "invite_code|bank_product|activity|misc",
      "type": "permanent|product|activity",
      "content": "...",
      "url": null,
      "expires_at": null,
      "source_group": "...",
      "source_sender": "...",
      "notes": "..."
    }
  ],
  "hot_leads_additions": [
    {
      "title": "...",
      "summary": "2-3 行描述",
      "category": "arbitrage|bug|personal_trick|gray_zone",
      "source_group": "...",
      "source_sender": "...",
      "risk_notes": "..."
    }
  ],
  "death_signals": [
    {
      "target_title_or_id": "...",
      "signal_text": "...",
      "signal_source": "...",
      "confidence": "high|medium|low"
    }
  ]
}
```

严格遵守上面的 fence 顺序和格式，不要在 fence 之间写多余解释。

## 去重规则（重要）

在产出 `permanent_additions` 时：
- 如果某个机会在「当前活跃的机会 → 永久库活跃条目」里已经出现（按标题、银行/产品名、URL 判断），**不要再次加入 `permanent_additions`**。同一个机会只应存在一条永久库记录。
- 标题尽量用稳定的规范化写法，不要为同一个机会生成「xx开放」「xx支持xx」「xx活动」等措辞不同的变体。
- 当今天聊天记录里有对已有条目的**负面/结束信号**（跑路、关停、额度收紧、风控出问题等），通过 `death_signals` 表达，而不是新建一条条目。
- 只有在出现**真正新的机会**时才加入 `permanent_additions`。
"""


def build_user_prompt(
    date: str,
    groups_with_content: list[tuple[str, str]],
    detail_path: str,
    active_permanent_summary: str = "",
    active_hot_leads_summary: str = "",
) -> str:
    """Build the user prompt.

    groups_with_content: list of (group_name, raw_markdown_export).
    detail_path: filesystem path to the detailed summary file (appended to concise version).
    active_permanent_summary / active_hot_leads_summary: existing DB context for death-signal detection.
    """
    groups_block = "\n\n".join(
        f"### === 来源: {name} ===\n\n"
        f"完整来源标签：{name}\n"
        f"精简来源标签：{concise_source_label(name)}\n\n"
        f"{content}"
        for name, content in groups_with_content
    )
    context = ""
    if active_permanent_summary or active_hot_leads_summary:
        context = f"""
## 当前活跃的机会（用于死亡信号检测）

### 永久库活跃条目
{active_permanent_summary or "(空)"}

### 热点板 14 天内活跃条目
{active_hot_leads_summary or "(空)"}
"""

    return f"""日期：{date}
详细版文件路径（精简版末尾要附这个路径）：{detail_path}

{context}

## 今日原始聊天记录

说明：每个来源块的标题中已经标明完整来源，例如 `微信 / 群名` 或 `Telegram / 群名`。
在精简版中不要分来源分块，每条重点末尾使用「精简来源标签 / 时间」，不要写平台名。
在详细版中可以保留完整平台来源。

{groups_block}
"""


def concise_source_label(source_name: str) -> str:
    """Return the group-only source label used in mobile summaries."""
    parts = [p.strip() for p in source_name.split("/")]
    if len(parts) >= 2 and parts[0] in {"微信", "Telegram"}:
        return " / ".join(parts[1:]).strip()
    return source_name.strip()
