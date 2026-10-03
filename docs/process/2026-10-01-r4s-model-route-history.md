# r4s 模型路线历史记录

2026-10-01 切换 CPA 前，运维指南包含以下部署说明。实际配置与调用结果见
[验证报告](../notes/2026-10-01-youtube-selection-verification.md)。

## 切换前的指南原文

**r4s 的 `~/chat-daily/config.yaml` 与 Mac 不同源、手工维护**：`deploy` 只 `git archive` 代码，`config.yaml` 与 `.env` 独立留在 r4s 的 `~/chat-daily/`。因 CLIProxyAPI（`127.0.0.1:8317`）在 r4s 上**不存在**，`models` 的两个端点都做了部署改写——**任何指向 `127.0.0.1:8317` 的别名在 r4s 上都不可达，必须改写成公网端点（经 bwg tinyproxy 出口）**：

| 用途 | r4s 端点 | model / key |
|---|---|---|
| `models.vision`（有封面卡的一句话导读，**主路径**） | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash` / `GOOGLE_API_KEY` |
| `models.summary`（无封面卡才触发的**文本兜底档**） | `https://api.vibekey.cn/v1` | `gpt-5.6-luna` / `VIBEKEY_API_KEY` |

`models.summary` 在 2026-07-18 前是 `*gemini` 锚点，指向 Mac 本机 `127.0.0.1:8317`——r4s 上连接被拒 → 异常被 summarizer 的 try/except 吞掉 → 那张卡直接**没有 📝 摘要行**（等于没有兜底，非报错，符合「投递优先于完美」故长期没暴露）。当天修复：改直连 VibeKey `gpt-5.6-luna`，并在 r4s `.env` 补 `VIBEKEY_API_KEY`（此前**缺失**，缺了会 `KeyError`、同样降级为无摘要行）。所以 **r4s `.env` 必须含 `VIBEKEY_API_KEY`**。验证：`python3 run_daily.py --bilibili-only` 能干净跑通（exit 0），或用带 `models.summary` 的 `LLMClient` 直调一次确认端点+key+代理三者可达。

