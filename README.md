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

- 代码：本仓库目录
- 数据：`~/chat-daily`

本地数据目录大致长这样：

```text
~/chat-daily/
├── config.yaml
├── permanent.jsonl
├── permanent.md
├── repeat_topics.jsonl
├── hot-leads/
├── archive/
└── logs/
```

其中：

- `archive/` 里是每天的原始导出和详细总结
- `hot-leads/` 里是最近 14 天内还活着的短期机会
- `permanent.jsonl` / `permanent.md` 是长期机会库
- `repeat_topics.jsonl` 是近 7 天重复话题库，用于把旧闻降权
- `logs/` 里是运行日志

## 运行前准备

你需要先准备好这几样东西：

1. 微信聊天导出能力
2. 已登录的 [`tg-cli`](https://github.com/public-clis/tg-cli)（Telegram 来源使用它的本地 SQLite）
3. 可用的大模型接口（默认使用 [DeepSeek API](https://api-docs.deepseek.com/)）
4. Telegram 机器人和 chat id

环境变量示例：

```bash
export CLIPROXY_API_KEY="..."
export CPA_API_KEY="..."
export DEEPSEEK_API_KEY="..."
export TG_BOT_TOKEN="..."
export TG_CHAT_ID="..."
```

也可以把这些变量写到本地数据目录的 `~/chat-daily/.env`。这个文件不属于代码仓库，建议权限保持 `600`。

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
      - "微信群 A"
      - "微信群 B"
  telegram:
    enabled: true
    db_path: "~/Library/Application Support/tg-cli/messages.db"
    sync_before_export: true
    chats:
      - id: "-100xxxxxxxxxx"
        name: "Telegram 群 A"
        limit: 500

llm:
  endpoint: "https://api.deepseek.com"
  model: "deepseek-v4-pro"
  api_key_env: "DEEPSEEK_API_KEY"
  max_tokens: 12000
  timeout: 600.0
  extra_body:
    reasoning_effort: "max"
    thinking:
      type: "enabled"

telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
```

旧版顶层 `groups:` 仍可读取，会自动当作 `sources.wechat.groups`。

## 手动运行

```bash
cd <repo>
source .venv/bin/activate
python run_daily.py
python run_daily.py --date 2026-04-17
```

## 测试

```bash
cd <repo>
source .venv/bin/activate
pytest -v
```

## 常见问题

**Telegram 收到的内容为什么看起来像“源码”？**

因为 Telegram 自己支持的格式跟普通 Markdown 不一样。如果直接把常见的标题、加粗符号塞进去，Telegram 往往不会按预期显示。这个仓库现在已经针对 Telegram 做了单独处理。

**为什么 `~/chat-daily` 里没有 git？**

因为那个目录存的是每天跑出来的数据，不是代码仓库。真正的代码在本仓库目录。

## 备注

这是一个个人本地项目，默认围绕 macOS、本地运行和你的现有环境来设计。
