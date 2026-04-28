# chat-daily-tg

每天自动导出微信和 Telegram 群消息，整理成一份统一日报发到 Telegram，同时把详细内容和长期值得跟踪的信息存到本地。

## 这是什么

这个项目每天早上会自动做 4 件事：

1. 读取指定微信群和 Telegram 群前一天的聊天记录
2. 生成一份适合手机看的统一简报
3. 把简报发到 Telegram
4. 把详细版、长期机会和短期热点保存到本地

适合用来长期跟踪：

- 群里的机会和活动
- 已经失效的路子
- 哪些信息值得继续观察

## 项目结构

这个仓库是“代码仓库”，真正每天产生的数据默认放在另一个目录：

- 代码：`/Users/Apple/Projects/chat-daily-tg`
- 数据：`/Users/Apple/chat-daily`

本地数据目录大致长这样：

```text
~/chat-daily/
├── config.yaml
├── permanent.jsonl
├── permanent.md
├── hot-leads/
├── archive/
└── logs/
```

其中：

- `archive/` 里是每天的原始导出和详细总结
- `hot-leads/` 里是最近 14 天内还活着的短期机会
- `permanent.jsonl` / `permanent.md` 是长期机会库
- `logs/` 里是运行日志

## 运行前准备

你需要先准备好这几样东西：

1. 微信聊天导出能力
2. 已登录的 `tg-cli`（Telegram 来源使用它的本地 SQLite）
3. 本地可用的大模型接口
4. Telegram 机器人和 chat id

环境变量示例：

```bash
export CLIPROXY_API_KEY="..."
export TG_BOT_TOKEN="..."
export TG_CHAT_ID="..."
```

## 配置

主要配置文件在：

```text
~/chat-daily/config.yaml
```

这里可以设置：

- 要监控的微信和 Telegram 群
- 每天几点跑
- 时区
- 使用哪个模型
- Telegram 机器人配置

多来源配置示例：

```yaml
sources:
  wechat:
    groups:
      - "贝利知识星球VIP群❤️"
      - "OpenCLI 交流群"
  telegram:
    enabled: true
    db_path: "~/Library/Application Support/tg-cli/messages.db"
    sync_before_export: true
    chats:
      - id: "-1003707563960"
        name: "CuiMao爱学习"
        limit: 500
      - id: "-1001162433032"
        name: "电丸朱氏会社"
        limit: 500

llm:
  endpoint: "https://api.moonshot.cn/v1"
  model: "kimi-k2.6"
  api_key_env: "KIMI_API_KEY"
  max_tokens: 16000
  timeout: 600.0

telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
```

旧版顶层 `groups:` 仍可读取，会自动当作 `sources.wechat.groups`。

## 手动运行

```bash
cd /Users/Apple/Projects/chat-daily-tg
source .venv/bin/activate
python run_daily.py
python run_daily.py --date 2026-04-17
```

## 测试

```bash
cd /Users/Apple/Projects/chat-daily-tg
source .venv/bin/activate
pytest -v
```

## Research loop

如果要像 `autoresearch` 一样长期做可记录实验，可以用本仓库的 research loop。

第一阶段不需要任何 API key，只用 fixture 和保存的样例输出验证解析、Telegram 渲染和结果记录：

```bash
python scripts/research_loop.py \
  --experiment-id offline-sample-html \
  --sample-output tests/fixtures/summary_output_sample.txt \
  --parse-mode HTML
```

真实模型实验默认也只是 dry-run，不会发送 Telegram。详细说明见 `docs/research-loop.md`。

## 现在已经解决的问题

- Telegram 发送时改成了真正适合 Telegram 的格式
- 微信和 Telegram 群消息会合并成同一份日报
- 手机端按信息价值统一排序，每条重点标注来源
- 不再把标题和加粗原样显示成 `###`、`**`
- 发送失败时会重试
- 每次运行后会把详细版落到本地

## 常见问题

**Telegram 收到的内容为什么看起来像“源码”？**

因为 Telegram 自己支持的格式跟普通 Markdown 不一样。如果直接把常见的标题、加粗符号塞进去，Telegram 往往不会按预期显示。这个仓库现在已经针对 Telegram 做了单独处理。

**为什么 `/Users/Apple/chat-daily` 里没有 git？**

因为那个目录存的是每天跑出来的数据，不是代码仓库。真正的代码在 `/Users/Apple/Projects/chat-daily-tg`。

## 备注

这是一个个人本地项目，默认围绕 macOS、本地运行和你的现有环境来设计。
