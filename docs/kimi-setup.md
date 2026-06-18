# Kimi (Moonshot) 作为 LLM 驱动的接入说明

本项目的 `LLMClient` (`src/wx_daily_tg/llm_client.py`) 走的是 OpenAI
兼容的 `/v1/chat/completions` 协议, Kimi 官方 API 原生兼容, **无需改
任何代码**, 只要改 `~/wx-daily/config.yaml` 里的 `llm` 段 + 设一个
环境变量即可。

## 区域端点

| 场景 | `endpoint` |
|---|---|
| 中国大陆账号 (platform.moonshot.cn 签发的 key) | `https://api.moonshot.cn/v1` |
| 海外账号 (platform.moonshot.ai 签发的 key) | `https://api.moonshot.ai/v1` |

两套账号体系独立, key 不通用。走错端点会返回
`401 Invalid Authentication`。

## 可用模型 (从 `/v1/models` 拉到的实测列表)

| model id | 上下文 | 是否 reasoning | 典型首响应 | 推荐场景 |
|---|---|---|---|---|
| `kimi-k2.6` | 262144 | 是 | 数百秒级 | 追求最强中文语义/Agent 能力, 可忍受延迟 |
| `kimi-k2-turbo-preview` | 262144 | 否 | 30-40s | **生产日报默认**, 快且结构化输出稳 |
| `kimi-k2-thinking` / `kimi-k2-thinking-turbo` | 262144 | 是 | 中-高 | 推理密集场景 |
| `kimi-k2.5` | 262144 | 是 | 中 | K2.6 备份 |
| `moonshot-v1-128k` | 131072 | 否 | 快 | 保守稳定, 低成本 |
| `moonshot-v1-auto` | 131072 | 否 | 自动 | 由平台路由 |

320 条/天的群聊量级在任何一款 256K 上下文模型上都游刃有余。

## 配置示例 (`~/wx-daily/config.yaml`)

```yaml
llm:
  endpoint: "https://api.moonshot.cn/v1"
  model: "kimi-k2-turbo-preview"   # 日常用, 换 kimi-k2.6 需同时调大 timeout
  api_key_env: "MOONSHOT_API_KEY"
  max_tokens: 16000
  timeout: 300                     # K2.6 请改为 600
```

```bash
export MOONSHOT_API_KEY="sk-..."   # 从 platform.moonshot.cn 控制台申请
```

## 端到端验证结果 (2026-04-24)

用合成的 2 群 12 条样本跑完整 `summarizer.run_summary` 链路:

| 模型 | 耗时 | 结构化 fence 解析 | perm+ / hot+ / dead |
|---|---|---|---|
| `kimi-k2.6` | 526s (2 次读超时后重试成功) | ✅ | 5 / 2 / 1 |
| `kimi-k2-turbo-preview` | 37s (1 次 503 后重试成功) | ✅ | 4 / 2 / 1 |

两者都能正确:

- 遵循三段 fence 格式 (`markdown concise` / `markdown detailed` / `json opportunities`)
- 从 "openai plus 低价卡密渠道挂了" 识别出死亡信号, 关联到已有 `permanent` 条目
- 抽取银行羊毛/签证/拉新/教育优惠等机会并分类

## 调优提示

- **reasoning 模型必须给够 `max_tokens`**: K2.6 在极小 budget 下会把
  所有 token 消耗在 `reasoning_content`, `content` 返回空串。保持默认
  的 `max_tokens: 16000` 即可。
- **K2.6 默认 300s 超时偏紧**: 实测会触发 2 次读超时后靠重试兜底,
  建议把 `llm.timeout` 提到 `600`。turbo 版用默认值没问题。
- **偶发 503**: 现有重试策略 (`retry.max_attempts: 3`,
  backoff `[5, 15, 60]`) 已经能兜住, 不用改。
- **费用**: 切到 Kimi 后会从原 CLIProxyAPI 零成本变为按 token 付费,
  建议先用 turbo 跑一周估算。

## 快速连通性自检

```bash
curl -sS https://api.moonshot.cn/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MOONSHOT_API_KEY" \
  -d '{"model":"kimi-k2-turbo-preview","messages":[{"role":"user","content":"ping"}],"max_tokens":32}'
```

返回 `choices[0].message.content` 非空即代表端点 + key + 模型
三者都可用。
