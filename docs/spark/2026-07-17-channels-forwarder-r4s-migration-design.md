# Spec: channels 转发迁 r4s

日期：2026-07-17
状态：可行性已论证，gating item（tg-cli 代理出网）已在 Mac 实测验证，**暂不实现**
来源：blindspot pass 三连对话（bot 接 agent → CLIProxyAPI 部署位 → channels 迁移可行性）。关联 spec：[bot 交互边界与消息精选采集](2026-07-17-interaction-boundary-curation-capture-design.md)（TG 内 LLM agent 不建的完整论证与 blindspot 存档在该文档，本文不重复）。

## 问题

Mac 剩余睡眠盲区「电池 + 合盖」会跳过 channels 转发的调度点（8 次/天），造成资讯延迟（不丢，恢复后 catch-up）。日报与 growth 因微信数据源引力（本机微信客户端数据库）搬不动；**channels 的负载全部是 Telegram，是 Mac 断供清单里唯一能搬走的件**。迁到 r4s 后资讯延迟消除，Mac 白天不必为 channels 保持清醒，disablesleep 的必要性收窄到 6:30 日报和 growth 三班。

## 核查事实（2026-07-17 实测，实现前需复核）

- **抓取链**：`telegram_exporter.sync_chat` 调外部 CLI `tg sync`（kabi-tg-cli v0.6.0，jackwener/tg-cli，Telethon 1.43，**MTProto 用户账号 session**）→ 写 `~/Library/Application Support/tg-cli/messages.db`（33MB sqlite）→ `raw_channels` 读库构卡。session 文件 `tg_cli.session` 与 db 同目录。
- **媒体链**：`scripts/tg_media_dump.py` 经 `TG_CLI_PYTHON` 解释器复用 `tg_cli.client.connect()`（同一 session）下载媒体 → Bot API multipart 直传。**一处 proxy patch 覆盖 sync 与 media dump 两条路径**。
- **tg-cli 原版无任何 proxy 支持**（site-packages 源码全文无 proxy/socks 字样），客户端构造单点在 `tg_cli/client.py` 的 `connect()`。
- **Telethon 1.43 的代理依赖是 python-socks 而非 PySocks**；未装 python-socks 时 proxy 参数被**静默忽略**（且因上游自身 UnboundLocalError bug 崩在 warn 行）——不做负向测试会得到"流量根本没走代理"的假阳性。
- L2 topic gate 的 delivered index 同样依赖 tg-cli session（`topic_dedup` 导入 `sync_chat`）；embedder（gemini 直连端点）走 `HTTPS_PROXY` env，挂了优雅降级不阻投递。
- **r4s 轨道现成**（`run_bilibili_r4s.sh` 模板）：git archive 部署到 `/root/chat-daily-tg`、数据目录 `/root/chat-daily`、`TZ=CST-8`、flock 防重入、tinyproxy 出口 `http://100.87.113.14:8888`（tailscale）、alert 函数、路由表 `/root/qwenproxy/.tg-notify-targets.json` 已同步在位。
- Bot token 多机共用只发不收无冲突；growth 的 `getUpdates` 独占权留在 Mac，不受影响。

## 验证记录：tg-cli proxy patch（2026-07-17，Mac，已通过）

- **Patch 形态**：环境变量开关 `TG_CLI_PROXY`（`scheme://host:port`，scheme ∈ http/socks5/socks4），解析成 `(scheme, host, port)` 字符串元组传 `TelegramClient(proxy=…)`——Telethon 的 `_parse_proxy` 原生认字符串 scheme，无需 PySocks 常量。变量不设返回 None，行为与原版逐字节一致（patch 后无变量基线已复测，launchd 生产轮不受影响）。
- **验证序列（五步全过）**：基线 OK → 假代理 `127.0.0.1:9` 失败于 connection refused（证明代理设置真的被采纳）→ bwg tinyproxy 认证成功（`get_me` 返回本人账号）→ `tg sync` 真实频道过代理全链路执行 → 无变量基线不变。**测试用的就是 r4s 未来的生产出口**（bwg tinyproxy over tailscale），Telethon-over-HTTP-CONNECT 确认可行。
- **易失性警告**：patch 现打在 uv tool venv 的 site-packages（`~/.local/share/uv/tools/kabi-tg-cli/...`），`uv tool upgrade kabi-tg-cli` 会冲掉；python-socks 亦装在该 venv。**落地时 fork 或给上游提 PR**，并把 python-socks 声明进依赖。任何环境重建后必须先重跑"假代理必须失败"的负向测试。

## 决策记录

1. **宿主选 r4s，否决 bwg**。纯网络视角 bwg 更优（负载全 TG、直连零跳、省双程流量），但抓取凭据是**用户账号 MTProto session = 账号接管级凭据**，必须留在家庭内网，不上公网 VPS。bilibili 选 r4s 是因为国内出口，channels 选 r4s 是因为凭据安全——理由不同，结论相同。
2. **r4s 新登录 session，不拷贝 Mac 的 `tg_cli.session`**。多 session 共存合法；拷贝旧 session 换出口 IP 有被风控吊销的先例；Mac 的 session 日报导出 TG 群还要用，必须保留。新 session 从 bwg 出口（美国 IP）登录，手机会收安全提醒，属预期。
3. **关联决策 A：CLIProxyAPI 暂不迁移**。8317 的全部消费者都在 Mac（config 三处 `127.0.0.1:8317` + qwenproxy MCP / cpa-image 等本机工具），Mac 关机时消费者同时关机——**代理与调用方可用性域完全重合**，迁移不解决任何真实断供，反而引入双跳链路（r4s）或凭据上公网（bwg）+ headless OAuth 运维。触发条件：出现 off-Mac 的 LLM 消费者（agent 立项或含 vision/judge 的管线迁出）。届时选 **bwg + 8317 绑死 tailscale 接口 + 双实例增设而非迁移**（Mac 本机保留回落，alias 一行切换），部署前先验证 OAuth 上游 refresh token 是否轮换互踢。
4. **关联决策 B：TG 内 LLM agent 不建**。完整论证（信任边界反转、批处理→常驻体制变更、部署死结）与 blindspot 存档（getUpdates 单消费者 409 静默失效、feedback inbox 污染、注入向量、多人指挥权）见[交互边界 spec](2026-07-17-interaction-boundary-curation-capture-design.md)，复活条件也记录在彼处。

## 设计：迁移方案

### 依赖清单（逐项判定）

| 依赖 | 现状 | 迁移动作 |
|---|---|---|
| 代码 | `--channels-only` 与 bilibili 同仓库 | 零成本，git archive 轨道现成 |
| 抓取（`tg sync`） | kabi-tg-cli + Telethon，无 proxy 支持 | fork/PR 上述已验证 patch；r4s 装 kabi-tg-cli + python-socks |
| 登录态 | Mac `tg_cli.session` | r4s 新登录（决策 2），`TG_CLI_PROXY` 过 tinyproxy |
| 消息库 | `~/Library/Application Support/tg-cli/messages.db` | r4s 重建，首次手动全量 sync；macOS 路径参数化 |
| 去重状态 | SeenStore 高水位 + L1 内容库 + L2 delivered index + dedup journal（`~/chat-daily/state/`） | **cutover 时整体拷贝**（见 P0） |
| L2 embedder | gemini 直连端点 | `HTTPS_PROXY` env 走 tinyproxy；优雅降级已内建 |
| 媒体 | `tg_media_dump.py` 同 session 下载 → Bot API 直传 | 同一 patch 覆盖；`TG_CLI_PYTHON` 路径参数化 |
| 推送 | Bot API sendMessage | 已验证轨道（bilibili 每日 24 轮走 tinyproxy） |
| 路由表 / .env / 告警 | r4s 已在位 | 零成本 |
| 调度 | launchd 8 次/天 | cron + flock（独立 lock 文件）+ `TZ=CST-8`，照抄模板 |

### 迁移序列

1. proxy patch 出 fork 或上游 PR（含 python-socks 依赖声明），消灭 uv venv 手改的易失性。
2. r4s 装依赖（telethon 纯 Python；先跑一遍 channels 相关模块 import 审计确认 FriendlyWrt python3 装得齐）→ `tg` 新 session 登录（过 tinyproxy）→ 首次手动全量 sync（`sync_chat` 的 subprocess timeout 120s 大概率不够，手动分批）。
3. config 参数化（`db_path`、`TG_CLI_PYTHON`）+ `run_channels_r4s.sh` 仿 bilibili 模板 + `--no-push` 干跑与 Mac 侧输出对比。
4. **原子 cutover**：拷贝 state 目录 → Mac `launchctl bootout` channels label 与 r4s cron 上线同一时刻 → `install-launchd.sh` 改为不再安装 channels（仿 bilibili 排除注释，防止将来重跑安装脚本复活双跑）。
5. runbook 更新：`--resend` 执行地变 r4s；channels 归档改落 `/root/chat-daily/archive`（需要 Mac 副本则加 rsync 回传）。

## 风险分级

- **P0**（迁移事故级，方案里已写死对策）：双跑窗口重复推送（cutover 纪律 + install 脚本排除）；seen 状态不迁 → 回看窗口内全量重推洪水（state 整体拷贝）。
- **P1**：proxy patch 的 fork 维护成本（上游吃进 PR 最好）；新 session 初期对 flood limit 更敏感（首次全量 sync 放慢）；媒体字节经 bwg 双程 ≈ 4x 流量记账（盯 bwg 月度 quota）；macOS 路径参数化遗漏。
- **P2**：首次 sync 超 timeout（手动分批即可）；r4s python 依赖审计；archive 不再有 Mac 副本。

## Non-goals

- 日报、growth 不迁——微信数据源引力在，Mac + disablesleep + catch-up 仍是该约束下最优解。
- 本 spec 不含实施；动手前先复核「核查事实」一节（tg-cli 版本、r4s 环境可能已变）。
