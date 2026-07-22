# B站 UP 主视频与专栏订阅、❤️ 分析交接设计

**日期：** 2026-07-22  
**状态：** 已确认  
**涉及项目：** `chat-daily-tg`、`Podcast4Bot`  
**目标 UP 主：** 朝阳不吐不快（UID `109187896`）

## 1. 背景

`chat-daily-tg` 已在 r4s 上按 20–30 分钟随机间隔抓取 B站白名单 UP 主的新视频，发送到 Telegram 的 `bilibili` 话题。视频卡片发送成功后会按 write-after-send 语义写入 `media_sent_ledger.jsonl`，Mac 每分钟从 r4s 同步该 ledger；`Podcast4Bot` 监听订阅卡的 ❤️ reaction，通过 ledger 找回原始 URL，再把内容交接到播客话题做快筛与按需精读。

本次新增 UID `109187896`，并且不仅订阅其视频，还订阅其专栏。专栏卡片同样支持 ❤️ 交接，但分析输入是文章全文，而不是字幕或 ASR 转写。

## 2. 目标与成功标准

### 2.1 目标

1. 在 r4s 上订阅“朝阳不吐不快”的新视频与新专栏。
2. 首次启用时补推最近 48 小时内的新视频和专栏。
3. 专栏卡片发到现有 `bilibili` Telegram 话题。
4. 用户对专栏卡片新增 ❤️ 后，`Podcast4Bot` 抓取文章全文并生成快筛卡。
5. 用户可从快筛卡按需触发完整精读与 Markdown 笔记。
6. 复用现有调度、Telegram 路由、seen、sent ledger、reaction owner 校验、队列和笔记机制，不建立平行系统。

### 2.2 成功标准

- 新视频与新专栏均在正常轮询窗口内送达，且不会重复推送。
- 只有 UID `109187896` 启用专栏抓取，其他 B站白名单 UP 主行为不变。
- 卡片发送失败时不写 seen；下轮可重试。
- 专栏卡片的每个 Telegram `message_id` 均能通过 ledger 反查 canonical URL。
- 卡片刚发出便点 ❤️ 时，即使 Mac 尚未同步 ledger，也不会永久漏掉交接。
- 专栏走文章正文分析路径，不调用字幕抓取、音频下载或 ASR。
- 正文、快筛或精读任一增强阶段失败时，不影响视频管线，也不生成无来源的虚假分析。

## 3. 范围

### 3.1 本次包含

- 新增 UP 主 `109187896` 的视频订阅。
- 仅为该 UP 主启用专栏订阅。
- 专栏列表、详情、正文抓取和 48 小时回看。
- 视频与专栏统一的内部内容模型。
- 专栏 Telegram 卡片与 sent-ledger 写入。
- ❤️ reaction 的有界延迟重试。
- `Podcast4Bot` 的 B站专栏 URL 分类、正文缓存、快筛、精读、Q&A 和笔记。
- 单元测试、集成测试、r4s 受控部署和首次 48 小时补推。

### 3.2 本次不包含

- 其他 UP 主的专栏订阅。
- 普通文字动态、转发、投票、直播和图集动态。
- B站动态 feed 作为主数据源。
- 自动核验投资观点的事实真伪或提供投资建议。
- 自动对每篇专栏生成完整精读；必须由 ❤️ 后的快筛卡再按需触发。
- 修改独立的 CC98 `r4sbot` 项目。

## 4. 已验证事实与约束

1. 目标账号是“朝阳不吐不快”，UID 为 `109187896`。
2. r4s 现有 B站任务由 `chat-daily-tg` 提供，以 `due_gate + */5 cron` 实现 20–30 分钟随机轮询，并由 `flock` 防重入。
3. UID `109187896` 尚未出现在 r4s 当前 B站白名单中。
4. 视频 `medialist` 接口在国内直连环境可用，无需 Cookie 和 WBI。
5. 专栏列表接口 `GET /x/space/article?mid=<uid>` 已返回该账号的文章 ID、标题、发布时间和摘要。
6. 专栏详情接口存在限流行为：验证期间 r4s 返回过 `code=-509`。实现必须低频、缓存、有限退避；不以并发、代理或频繁换接口绕过限流。
7. 动态 feed 接口验证时返回 HTTP 412，不能作为本次稳定主路径。
8. Telegram `message_reaction` 不含原消息正文，必须依赖 `media_sent_ledger.jsonl` 解析 URL。
9. r4s ledger 到 Mac 的同步周期约一分钟，存在“reaction 先到、ledger 后到”的竞态。
10. B站请求必须 `trust_env=False` 直连；海外代理可能触发风控。Telegram 与 LLM 仍使用既有代理出口。

## 5. 方案选择

### 5.1 采用：扩展现有 B站订阅与 Podcast4Bot 内容类型

- 在 B站白名单单项上增加 `articles` 开关。
- 在现有 B站轮询中增加低频专栏列表抓取。
- 视频和专栏共用调度、发送端、seen 和 ledger，但使用明确的内容类型与各自的渲染/抓取适配器。
- `Podcast4Bot` 把输入抽象为 media/article 两种 source，不把文章正文伪装成转写。

这是最小且边界清晰的方案：基础设施复用，媒体和文章的获取机制保持隔离。

### 5.2 不采用：独立专栏守护进程

独立调度与状态隔离更强，但目前只有一个 UP 主启用专栏，会重复 cron、告警、Telegram sender、seen 和部署逻辑，维护成本高于收益。

### 5.3 不采用：统一改用动态 feed

动态 feed 理论上可一次覆盖视频、专栏和普通动态，但已出现 HTTP 412，数据类型复杂且会扩大本次范围。视频继续使用已验证的 medialist，专栏使用文章接口。

## 6. 总体架构与数据流

```text
r4s / chat-daily-tg
  due_gate (20–30 min) + flock
    ├─ video adapter: medialist(uid)
    └─ article adapter: article list(uid, articles=true)
          ↓
    BilibiliContent normalization
          ↓
    lookback + SeenStore filtering
          ↓
    video/article card rendering
          ↓
    Telegram bilibili topic
          ↓ send success
    media_sent_ledger.jsonl + SeenStore
          ↓ every minute
Mac ledger-sync
          ↓
Podcast4Bot message_reaction listener
          ↓ owner + emoji + source validation
    ledger hit ────────────────┐
    ledger miss → pending retry│
                              ↓
                      URL/content routing
                       ├─ media → subtitle/ASR
                       └─ article → full text
                              ↓
                    podcast topic triage card
                              ↓ user clicks 📖
                       full analysis + note
```

## 7. chat-daily-tg 设计

### 7.1 配置

扩展 `BilibiliUp`：

```python
class BilibiliUp(BaseModel):
    uid: int
    name: str | None = None
    articles: bool = False
```

r4s 运行配置新增：

```yaml
sources:
  bilibili:
    fetch:
      whitelist:
        - uid: 109187896
          name: 朝阳不吐不快
          articles: true
```

`articles` 默认 `false`，因此现有白名单 UP 主不会突然产生专栏推送。

### 7.2 统一内容模型

新增内部模型，避免在视频结构上不断加文章专用空字段：

```python
@dataclass(frozen=True)
class BilibiliContent:
    kind: Literal["video", "article"]
    content_id: str             # BV... 或 cv...
    title: str
    author: str
    uid: int
    url: str
    publish_time: datetime | None
    summary: str = ""
    cover: str | None = None
    description: str = ""
```

视频适配器可以从现有 `BiliVideo` 渐进转换，不要求一次性重写所有视频逻辑。发送层按 `kind` 分派到 video/article renderer。

### 7.3 专栏抓取

#### 列表

对 `articles=true` 的 UP 调用：

```text
GET https://api.bilibili.com/x/space/article
  ?mid=109187896
  &pn=1
  &ps=<bounded_limit>
  &sort=publish_time
```

规则：

- `httpx.Client(..., trust_env=False)`。
- 串行请求，不为单个 UP 并发分页。
- 仅取足以覆盖 48 小时窗口的第一页；只有页面最旧项仍在窗口内且 `has_more` 时才继续，页数设硬上限。
- 解析 `id`、`title`、`publish_time`、`summary`、封面。
- 条目脏数据逐条隔离；非法 ID、非法时间戳或空标题跳过并记录。

#### 正文与详情

正文不在每次定时轮询中预抓。r4s 推送卡只依赖列表元数据；用户点 ❤️ 后才由 Mac 的 `Podcast4Bot` 按需抓全文，降低 `-509` 风险与无效流量。

`Podcast4Bot` 的正文获取顺序：

1. 尝试 `GET /x/article/view?id=<article_id>`，要求 `code == 0` 且解析出非空正文。
2. 若接口结构变化或正文缺失，回退到文章页 `https://www.bilibili.com/read/cv<id>`，从稳定嵌入状态或正文 DOM 提取。
3. 对 `-509`、HTTP 429、412 使用有限退避；不并发轰击，不切海外代理绕过。
4. 成功后缓存原始快照和清洗文本，后续快筛、精读、Q&A 不再重复访问 B站。

### 7.4 回看和去重

统一回看窗口为 48 小时。

```text
视频：bilibili:<bvid>
专栏：bilibili:article:<article_id>
```

- 发送成功后才写 seen。
- `--no-push` 不写 seen。
- 同轮结果按 `kind + content_id` 去重。
- 首次启用不播种基线，允许补推最近 48 小时内容。
- 超过窗口的历史文章不推送，也不需要写 seen。

### 7.5 专栏卡片

卡片正文：

```text
📄 专栏
<b>标题</b>
👤 朝阳不吐不快 · 发布时间
📝 原始摘要或清洗后的短预览

❤️ 标记后发送到 Podcast4Bot 分析
```

按钮：

```text
[ 📖 阅读全文 ]
```

行为：

- 有封面时优先 `send_photo`。
- 封面下载或 `sendPhoto` 失败时降级文本卡片和链接预览。
- 摘要为空时不生成臆测性替代内容，可用正文元数据中的明确预览；仍为空则只发标题和链接。
- 卡片成功发送后写 ledger，再写 seen；ledger 写失败不得把已发送卡片重发，但必须记录高优先级日志/告警，因为 ❤️ 解析能力受损。

### 7.6 sent ledger

每个成功返回的 Telegram `message_id` 写一行：

```json
{
  "chat_id": -1000000000000,
  "message_id": 12345,
  "thread_id": 486,
  "url": "https://www.bilibili.com/read/cv51618753",
  "producer": "bilibili",
  "id": "bilibili:article:51618753"
}
```

canonical URL 固定为 `https://www.bilibili.com/read/cv<id>`。若卡片降级产生多条消息，所有可被 reaction 的消息 ID 都必须写 ledger。

## 8. Podcast4Bot 设计

### 8.1 URL 与内容类型识别

扩展 B站 URL 解析：

```text
/video/BV...  → platform=bilibili, content_kind=media
/read/cv...   → platform=bilibili, content_kind=article
/opus/...     → 若能规范化到 article id，则 content_kind=article；否则拒绝
```

canonical URL 必须保留文章身份：

```text
https://www.bilibili.com/read/cv51618753
```

不能把所有 `bilibili.com` URL 都交给 yt-dlp。

### 8.2 统一 source_text 接口

把快筛、精读和问答对固定 `transcript` 的依赖抽象为：

```python
@dataclass(frozen=True)
class AnalysisSource:
    kind: Literal["media", "article"]
    text: str
    title: str
    author: str
    url: str
    metadata: dict
```

- media：`text` 来自字幕或 ASR，并保留时间戳。
- article：`text` 来自清洗后的文章正文，并保留标题/段落层级和图片说明。
- prompt 模板接收 `source_text` 与 `source_kind`；文章 prompt 不出现“转写”“时间戳”或“音频”。

### 8.3 文章缓存

```text
articles/<key>.html       # 可选原始响应快照，便于解析回归
articles/<key>.txt        # 清洗后、供 LLM 分析的正文
transcripts/<key>.*       # 继续只用于 media
outputs/<key>-triage.md
outputs/<key>-digest.md
notes/<date> <title> [<key>].md
```

缓存只有在内容通过最低完整性检查后才原子写入。空正文、登录页、限流页或错误 JSON 不得污染缓存。

### 8.4 正文清洗与完整性

保留：

- 标题和章节层级。
- 正文段落。
- 有序/无序列表。
- 引用块。
- 图片说明、alt 文本或紧邻说明文字。
- 明文链接及其锚文本。

移除：

- 导航、推荐、评论、登录提示、分享按钮。
- 脚本、样式和跟踪字段。
- 重复标题及重复段落。

完整性门槛：

- HTTP/API 成功不足以视为正文成功。
- 清洗结果必须达到合理最小长度，并含标题或若干实质段落。
- 若接口给出 `words`，清洗长度与其严重不符时视为可疑并尝试 fallback。
- 超长文章按章节均匀取样，覆盖开头、中部和结尾；不能只截开头。

### 8.5 ❤️ reaction 与 pending 重试

现有 reaction 验证继续生效：

- 只接受配置 owner。
- `PODCAST_OWNER_IDS` 为空时 fail-closed。
- 只接受新增的 `❤️`/`❤`。
- 只接受已知 producer 或已登记的 B站/YouTube 订阅话题。

ledger miss 时不立即丢弃：

```json
{
  "chat_id": -1000000000000,
  "message_id": 12345,
  "user_id": 67890,
  "first_seen_at": 1780000000,
  "next_attempt_at": 1780000015,
  "attempt": 0
}
```

建议节奏：15 秒、45 秒、90 秒。要求：

- 状态持久化到现有 `jobs.json` 或独立原子状态文件。
- daemon 重启后继续未过期任务。
- 以 `(chat_id, message_id)` 幂等；重复 reaction 不创建重复任务。
- 找到 ledger 后删除 pending 并进入正常 `enqueue_link`。
- 最后一次仍未找到时终止，不无限重试；记录包含 message ID 的明确日志。
- 若 ledger 解析出的 URL 不受支持，终止并记录，而不是反复重试。

### 8.6 快筛流程

❤️ 成功交接后：

1. 在播客话题发送收条，注明来源为 B站订阅。
2. 抓取或读取缓存的专栏全文。
3. 生成快筛卡。
4. 保留现有按钮：`📖 获取精读` / `🗑 跳过`。

文章快筛应包含：

- 一句话结论。
- 核心论点。
- 关键证据或数据。
- 作者的关键假设、立场或明显遗漏。
- 是否值得阅读全文及理由。

这些是分析框架，不是要求模型断言文章观点为真。涉及投资、市场和个股的内容应明确区分“作者主张”“文中证据”和“分析器判断”。

### 8.7 按需精读

点击 `📖 获取精读` 后复用现有 digest 状态机，输出：

- 文章中心主张。
- 论证结构。
- 核心事实、数据和文中出处。
- 事实判断与价值判断的区分。
- 隐含前提。
- 可能的反例与其他解释。
- 对现实决策的启发。
- 不确定性和待核验事项。
- 原文链接。

生成 Markdown 笔记并保存在 `Podcast4Bot/notes/`。精读完成后保留快筛卡，把按钮改为 `✅ 已精读 / 🗑 略读`。

### 8.8 Q&A

文章全文准备完成后，该专栏成为播客话题的 `last_key`，用户可直接追问。回答必须以缓存正文为主要证据；正文没有覆盖的事实应明确说“不在原文中”，不能把模型常识伪装成文章内容。

## 9. 状态、幂等和失败语义

| 阶段 | 成功状态 | 失败行为 |
|---|---|---|
| 专栏列表抓取 | 得到规范化条目 | 单 UP 专栏失败不阻塞视频；告警节流 |
| 卡片发送 | Telegram 返回 message ID | 不写 seen，下轮重试 |
| ledger 写入 | 所有 message ID 已写 | 卡片不重发；高优日志/告警 |
| seen 写入 | 内容进入已发送集合 | 写失败需告警，避免下轮重复 |
| reaction lookup | 找到合法 URL | miss 进入有界 pending，不直接丢弃 |
| 正文抓取 | 完整性校验通过并原子缓存 | 快筛收条显示失败，可稍后重新 ❤️/重试 |
| 快筛 | 输出缓存并发卡 | 保留正文缓存，不自动生成精读 |
| 精读 | 输出缓存、笔记和 TG 消息 | 保留快筛卡，允许再次触发 |

同一 canonical URL 使用稳定 SHA-1 key；已有 `queued`、`triaged`、`digesting` 或 `digested` 状态时，重复 ❤️ 应返回已有任务/卡片链接，而不是重复处理。

## 10. 错误处理与安全边界

### 10.1 B站与网络

- 所有 B站 API、文章页和封面请求 `trust_env=False`。
- `-352` 视为 IP 风控并中止对应抓取轮次。
- `-509`、429、412 使用有界退避；持续失败则告警和停止，不绕过。
- 单条脏数据、坏 HTML、缺封面不能杀死整轮。
- 正文获取按需进行，避免每 20–30 分钟重复抓全文。

### 10.2 LLM 信任边界

- prompt 输出结构必须有 code-level 解析和缺字段兜底。
- 文章内容视为不可信输入；其中任何“忽略指令”“调用工具”等文本都只是待分析正文，不能改变系统行为。
- 快筛与精读失败时不得用列表摘要冒充全文分析。
- 投资类专栏不输出确定性收益承诺，不把作者观点包装为系统建议。

### 10.3 Telegram 与权限

- reaction 必须由 owner 触发。
- 不从 reaction update 猜 URL，ledger 是唯一机器可用映射。
- token 只从既有 `.env` 读取，日志继续压制包含 Bot API token 的 httpx URL。
- 不新增对外可写接口。

## 11. 验证计划

### 11.1 chat-daily-tg 单元测试

- `BilibiliUp.articles` 默认 false。
- 只有显式启用的 UP 调用专栏列表。
- 专栏列表正常、空列表、脏字段、非零 code、HTTP 超时、`-509`。
- 48 小时边界前后条目。
- 视频与文章去重键不冲突。
- `--no-push` 不写 seen/ledger。
- 图片发送失败降级文本卡。
- 发送失败不写 seen。
- 专栏卡所有 message IDs 都写 canonical article URL。

### 11.2 Podcast4Bot 单元测试

- `/video/BV...` 与 `/read/cv...` 正确区分。
- canonical article URL 稳定。
- article 不进入字幕、下载或 ASR 函数。
- API 正文解析与 HTML fallback。
- 限流页、登录页、空正文不写缓存。
- HTML 清洗保留标题、列表、引用、图片说明并移除脚本/导航。
- 超长文章取样覆盖头中尾。
- ledger miss 创建 pending；15/45/90 秒后解析成功。
- pending 重启恢复、最终过期、重复 reaction 幂等。
- 非 owner、错误 emoji、未知 producer、未知 URL 被拒绝。
- article 快筛、精读、Q&A 使用 source_text 而非 transcript。

### 11.3 集成验证

1. 在本地 fixture 上运行两仓库全量测试。
2. r4s dry-run 验证 UID `109187896` 的视频和专栏候选。
3. 停止真实推送前不写 seen；确认 48 小时候选数量和标题。
4. 受控真实运行，补推最近 48 小时内容。
5. 核对 B站话题中的视频卡与专栏卡。
6. 核对 r4s ledger 行和一分钟内的 Mac 同步结果。
7. 对专栏卡点 ❤️，确认播客话题出现收条与快筛卡。
8. 在卡片发送后、ledger 同步前立即点 ❤️，确认 pending 最终成功交接。
9. 点击 `📖 获取精读`，确认完整分析、快筛卡保留及 Markdown 笔记生成。
10. 再次添加 ❤️ 或重复交接，确认只返回已有结果而不重复分析。

## 12. 部署与回滚

### 12.1 部署顺序

1. 先部署 `Podcast4Bot` 的 article 路由和 pending reaction 支持，使消费者向后兼容。
2. 重启并验证 Podcast4Bot daemon。
3. 再部署 `chat-daily-tg` 到 r4s。
4. 更新 r4s 白名单，为 UID `109187896` 设置 `articles: true`。
5. dry-run 后受控真实运行，完成 48 小时补推。
6. 观察至少一个完整轮询周期和一次 ❤️ 快筛/精读流程。

### 12.2 回滚

- 将该 UP 的 `articles` 改回 false：停止新专栏推送，视频订阅保留。
- 从白名单移除 UID：视频和专栏均停止。
- 回滚 r4s 代码不删除 seen/ledger，避免恢复后历史重复推送。
- 回滚 Podcast4Bot 时保留文章缓存、快筛、笔记和 pending 状态备份。
- 无需恢复 Mac B站 launchd；B站 digest 仍只能由 r4s 调度，避免双跑。

## 13. 实施边界

实现将跨两个仓库进行，但保持以下边界：

- `chat-daily-tg` 负责发现内容、卡片投递、seen 和 ledger。
- `Podcast4Bot` 负责 reaction 消费、全文获取、分析、Q&A 和笔记。
- 两者只通过 Telegram 事件、canonical URL 和 append-only ledger 耦合。
- 不让 r4s 承担文章 LLM 精读，也不让 Mac 定时重复抓取全部专栏。
