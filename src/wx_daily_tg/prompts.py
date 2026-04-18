from __future__ import annotations

SUMMARIZER_SYSTEM = """你是一个薅羊毛/理财/套利信息分析助手。
你的任务：对用户提供的多个微信群一天的聊天记录做结构化总结。

输出要求：两份 markdown + 一份 JSON，用三个 fence 分隔，顺序固定：

第一个 fence：
```markdown concise
(给 Telegram 手机端用的精简版，≤1500 字)
结构：
### 🗓️ 日期概览
(2-3 句话总述今天 N 个群的整体内容)

### 📌 值得关注
- 类型 | 内容 | 出处（群+人+时间）

### 🚨 死亡信号（若有）
- xxx 被标记为 dead（原文引用）

末尾附一行：详情：<path>
```

第二个 fence：
```markdown detailed
(给本地 md 档案的详细版，无长度限制)
结构：
## 群 1: <群名>
<2-3 句话总结 + 主干脉络>

## 群 2: ...

## 跨群合并话题
...

## 值得关注清单（完整表格）
...

## 人物画像
(主要贡献者的一句话评价)
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
        f"### === 群: {name} ===\n\n{content}" for name, content in groups_with_content
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

{groups_block}
"""
