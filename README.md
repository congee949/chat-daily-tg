# wx-daily-tg

每天自动导出微信群消息，整理成一份简短日报发到 Telegram，同时把详细内容和长期值得跟踪的信息存到本地。

## 这是什么

这个项目每天早上会自动做 4 件事：

1. 读取指定微信群前一天的聊天记录
2. 生成一份适合手机看的简报
3. 把简报发到 Telegram
4. 把详细版、长期机会和短期热点保存到本地

适合用来长期跟踪：

- 群里的机会和活动
- 已经失效的路子
- 哪些信息值得继续观察

## 项目结构

这个仓库是“代码仓库”，真正每天产生的数据默认放在另一个目录：

- 代码：`/Users/Apple/Projects/wx-daily-tg`
- 数据：`/Users/Apple/wx-daily`

本地数据目录大致长这样：

```text
~/wx-daily/
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
2. 本地可用的大模型接口
3. Telegram 机器人和 chat id

环境变量示例：

```bash
export CLIPROXY_API_KEY="..."
export TG_BOT_TOKEN="..."
export TG_CHAT_ID="..."
```

## 配置

主要配置文件在：

```text
~/wx-daily/config.yaml
```

这里可以设置：

- 要监控的群名
- 每天几点跑
- 时区
- 使用哪个模型
- Telegram 机器人配置

## 手动运行

```bash
cd /Users/Apple/Projects/wx-daily-tg
source .venv/bin/activate
python run_daily.py
python run_daily.py --date 2026-04-17
```

## 测试

```bash
cd /Users/Apple/Projects/wx-daily-tg
source .venv/bin/activate
pytest -v
```

## 现在已经解决的问题

- Telegram 发送时改成了真正适合 Telegram 的格式
- 不再把标题和加粗原样显示成 `###`、`**`
- 发送失败时会重试
- 每次运行后会把详细版落到本地

## 常见问题

**Telegram 收到的内容为什么看起来像“源码”？**

因为 Telegram 自己支持的格式跟普通 Markdown 不一样。如果直接把常见的标题、加粗符号塞进去，Telegram 往往不会按预期显示。这个仓库现在已经针对 Telegram 做了单独处理。

**为什么 `/Users/Apple/wx-daily` 里没有 git？**

因为那个目录存的是每天跑出来的数据，不是代码仓库。真正的代码在 `/Users/Apple/Projects/wx-daily-tg`。

## 备注

这是一个个人本地项目，默认围绕 macOS、本地运行和你的现有环境来设计。
