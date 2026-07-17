# Spec: bot 交互边界与消息精选采集

日期：2026-07-17
状态：方向已确认，**暂不实现**——阶段 0 零代码先跑，4 周后凭数据过 Go/No-Go
来源：blindspot pass 对话（slash command 是否保留 → 是否接 TG 内 LLM agent → 采集手势演化：302 埋点 → Saved Messages → reaction 精选）

## 问题

bot 定位是通知推送（消息中转站），引出三个纠缠在一起的问题：

1. BotFather 注册的 slash command 菜单是否保留（代码里无任何 handler）
2. 是否接入 LLM agent 让交互更方便
3. 对感兴趣内容如何低摩擦标记，并接入"Claude Code 加工 → 知识库沉淀"的后续链路

## 核查事实（2026-07-17 实测，实现前需复核）

- **bot 已非纯发送**：`growth_weekly.poll_dm_feedback` 每天在 growth 任务尾部排空 `getUpdates` → `feedback-inbox.jsonl`，周六消费合并 rubric；当前 `allowed_updates=["message"]`。
- **getUpdates 单消费者**：同一 token 只允许一个消费者（与 webhook 互斥）。`growth_weekly.py` 已有 409 处理，但失败模式是**静默返回 0**——反馈断流无表象。
- slash command 全仓库无分发逻辑，仅是 BotFather 菜单项。
- `~/chat-daily/archive/YYYY/MM/DD/` 已是永久原文库（summary.md + raw text，`archive.py` 注释明示 text archive 是 permanent record）；另有 `permanent.jsonl` 永久机会库。
- `tg_sender.py` 各发送函数**已返回 message_id**（221/389/430/478/527/590 行），但调用方未持久化成可反查台账。
- **消息粒度**：日报正文按 3900 字符在换行处切块（`split_message`），块边界由字符数决定、与条目无关；频道转发、growth 卡片、健康卡是逐条独立消息。
- Bot API 7.0+ 的 `message_reaction` 更新：只含 chat/message_id/user/old+new reaction，**不含消息内容**；群聊内 bot 须为管理员才能收到；`allowed_updates` 在 TG 侧是**有状态参数**（不传沿用上次设置）。

## 决策记录

1. **TG 内 LLM agent：不建**。
   - 信任边界反转：用户文本 → LLM 解析 → 执行动作（resend/补跑/改配置），违反"LLM 输出必须 code-level 兜底"硬规则；动作层必然退化为"LLM 提议 + 确定性确认"，LLM 只剩自然语言糖衣。
   - 体制变更：全部可靠性机制（launchd 同 label 防重入、flock、day marker、hb-wrap）是批处理形状，常驻交互进程需要一套不存在的守护机制。
   - 部署死结：动作执行依赖 Mac 本地数据与 CLIProxyAPI（127.0.0.1:8317），而 Mac 有「电池+合盖」睡眠盲区——随机离线的交互功能比没有更糟。
   - 想要的 retrieval agent 已存在：Claude Code 跑在 Mac 上，对 archive/dedup journal/growth segments 有完整文件系统访问权。TG 内再造一个是更弱的复制品。
   - 复活条件：阶段 0 数据证明内容追问高频存在，且上述前提逐条解决。
2. **职责边界四层模型**：TG bot = 注意力分配；`~/chat-daily/archive` = 原文库；Claude Code/Codex = 加工；Obsidian vault = 沉淀。消息站不长沉淀能力，交互发生在工具所在处而非消息所在处。
3. **slash command 菜单清空：已完成**（2026-07-17 经 BotFather 清空，`getMyCommands` 四 scope——default/all_private_chats/all_group_chats/all_chat_administrators——验证全空，无残留）。
4. **302 埋点、微信归档网页：出局**。标记通道本身就是测量仪器——clips 行数 = 明确的处理意图计数，比点击埋点（可能只是好奇）更准。
5. **采集手势三阶段**：Saved Messages（现在，零代码）→ 转发 bot DM 分流（阶段 1）→ reaction 双轨（阶段 2 终态）。

## Blindspot 存档（agent 方案的 unknown unknowns，供未来复议时查阅）

- **P0** getUpdates 单消费者：新增监听会让 growth 反馈排空持续 409 静默失效，数周后才被发现。
- **P0** 信任边界反转（见决策 1）。
- **P1** 批处理 → 常驻进程的体制变更成本。
- **P1** 部署位置死结（Mac 睡眠 vs 数据/代理绑定）。
- **P1** feedback inbox 数据污染：指挥 agent 的命令话语与 DM 反馈同信道，会被周六 rubric 当反馈消费。
- **P2** 注入向量：日报内容来自不受信源群，agent 若既读上下文又有操作权，源群消息成为 prompt injection 载体。
- **P2** 多人指挥权：通知群非单人（fming、@Congee123 在入组流程中），群内应答需 user id 白名单收死。

## 设计

### 阶段 0（现在，零代码）

- 感兴趣条目转发到 **Saved Messages** 作为待处理队列。
- **禁止转发到 bot DM**：现排空逻辑会把任何 DM 文本写进 growth feedback-inbox，污染周六 rubric——分流写好前 bot DM 信箱被反馈语义占用。
- 纪律：每周 Claude Code 会话第一件事「处理收藏」——逐条展开原文沉淀进 vault 或明确丢弃，处理完即从收藏删除，保持队列不成坟场（收藏动作会制造"已处理"错觉，是此类方案的头号失败模式）。
- 测量：每周清空时记一下条数，即需求信号。

### 阶段 1（Go 之后）：排空分流 + clips.jsonl

- `poll_dm_feedback` 加确定性分流：识别「转发自 bot 自身的消息」→ 写 `~/chat-daily/state/clips.jsonl`；其余 DM 文本 → feedback-inbox 照旧。顺带解决反馈信箱污染。
- 标记习惯从 Saved Messages 1:1 平移为转发 bot DM（手势不变，换个目标聊天）。
- 复用既有每日 poll：无常驻进程、无第二消费者、动作链路无 LLM。

### 阶段 2（终态）：reaction 精选双轨

- 排空点 `allowed_updates` 加 `"message_reaction"`——有状态参数，必须保证全系统唯一排空点在设置它。
- bot 提升为群管理员（前置确认）；按 user id 过滤 reaction（若开放为协作精选，是显式产品决策而非默认）。
- 发送侧补 **sent-ledger**：发送函数已返回 message_id，落盘 `message_id → 内容来源` 映射供反查。
- reaction 可取消：事件带 old/new，取消即注销标记，处理幂等。
- **适用边界**（粒度决定）：

| 内容流 | 消息粒度 | reaction 精选 |
|---|---|---|
| 频道转发 | 1 消息 = 1 条目 | ✅ 主轨 |
| growth 卡片 / 健康卡 | 独立消息 | ✅ |
| 日报正文 | 3900 字符任意切块 | ❌ 粒度错配，不为此重构日报发送形态 |

- 日报条目继续走 DM 转发轨；两轨汇入同一 `clips.jsonl`。

### 验收标准（阶段 1+2 实现时）

1. growth 反馈与 clips 互不污染：转发 bot 消息不进 inbox，普通反馈不进 clips。
2. 对频道转发消息点 reaction 后，clips.jsonl 出现可反查到源内容的记录；取消 reaction 对应注销。
3. 非白名单成员的 reaction 不产生记录（除非显式开启协作精选）。
4. getUpdates 仍只有一个消费点；growth 反馈通路回归验证（周六 rubric 合并正常）。
5. Claude Code 会话可凭 clips.jsonl 条目定位到 archive 原文上下文。

## Go/No-Go 判据

- 4 周 Saved Messages 数据：每周稳定有标记（≥3 条/周量级）且清空仪式在执行 → Go 阶段 1。
- 连续数周 0 条或积压不清 → 「读完即走」才是真实消费模式，全链路关闭，问题解散。

## 不做的事（YAGNI）

TG 内 LLM agent、302 中转埋点、微信归档网页、日报逐条化重构、webhook、常驻监听进程、MTProto 用户会话读 Saved Messages。
