# 外部接口合同

以下是适配器必须实现的语义。除“兼容导出”明确列出的格式外，字段命名、传输协议与内部对象表示可由新实现选择。

## 采集接口

请求包括来源身份、业务时区、时间窗或游标、单次预算与附件策略。响应包括有序 SourceItem 集合、已完整覆盖范围、下一页定位、源状态和未覆盖原因。页为空、源无内容、权限失效、限流、源离线、解析失败必须可区分。

每条输入保留正文与 caption、源/采集时间、稳定 ID、来源与内容链接、分组成员、媒体种类/可访问性。源编辑产生修订；源删除保留 tombstone 与已有证据的保留政策。日期边界不依赖进程本地时区，游标不能只用时间戳而丢掉同一时刻的多条消息。

接入本地 CLI/数据库时要声明版本、可用命令、输出格式、共享 session/文件锁以及读写副作用。不能同时让多个来源 worker 无限制操作同一个 Telegram session 或微信 daemon。旧本地路径通过迁移 profile 注入，不能写死在新领域逻辑里。

## Telegram 出站

请求传递固定目标、内容部件、格式、媒体、回复目标和可选按钮。响应转换为 [投递结果](03-domain-and-delivery.md#delivery-state)。格式解析明确失败时可转纯文本；媒体明确失败时使用文字退路；ambiguous 不触发另一种格式的再次发送。

文本分块、caption、相册和富消息限制由适配器的能力表承担，附官方依据与验证日期。初始化或升级时测试边界，避免在多个业务模块重复常量。[Bot API](https://core.telegram.org/bots/api#available-methods) 是协议来源，不是新系统性能保证。429 使用服务给出的等待建议，并受本次总重试预算约束，见 [ResponseParameters](https://core.telegram.org/bots/api#responseparameters)。

目标凭据、Bot 身份与 chat/topic 必须匹配配置。错误日志移除 token、请求认证字段和可能携带凭据的 URL。发送兼容层不得回传密钥给网页。

## Telegram 入站与共享消费者

一个 Bot 的 update 消费入口有唯一 owner。可以是本系统，也可以是已有外部 relay；不能为成长周报、点赞或人工复核另起竞争消费者。接入前先查询现有 webhook/consumer 所有权，启动冲突要明确报错。

收到一批 update 后先幂等持久化 inbox，再提升 offset 或确认处理。相同 update ID 与不同 payload 视为冲突，整批不得静默跳过。业务 handler 失败留 pending；重复投递不重复消费。回调验证 actor、消息、case、按钮动作与原任务绑定，未授权事件不改变状态。

`getUpdates` 与 webhook 互斥；offset 高于 update ID 就会确认此前更新。具体机制见 [官方入站协议](https://core.telegram.org/bots/api#getupdates)。reaction/callback 订阅和所需权限在接入配置中声明；不能用“无事件”代替“已验证可收事件”。

<a id="ledgers"></a>
## 兼容账本

兼容文件为 UTF-8 JSONL，一行一个映射。完整原始文件留作导入证据；坏行、截断尾行与冲突另列隔离报告。成功接入不能凭空补齐不存在的回执。

### 媒体消息映射

用于从 Telegram 卡片找到 B站/YouTube 等内容。每个目标消息一行，包括相册和分块产生的多个消息。

| 字段 | 旧格式语义 |
|---|---|
| chat_id、message_id | Telegram 目标，整数 |
| thread_id | 可选话题 ID |
| url | 内容的规范 URL |
| producer | 内容生产者，例如 bilibili、youtube |
| ts | 有时区的投递记录时间；旧无时区值按导入配置处理并记解释依据 |
| id | 可选内容 ID；缺失时不得随意拿目标消息 ID 替代 |

此旧格式没有 schema 字段。由导入任务的格式版本区分，不应冒充 `sent-content.v1`。媒体映射本身只证明旧记录声称存在送达映射；新系统 confirmed 的证据要求见迁移文档。

### 通用已投递正文

| 字段 | 旧格式语义 |
|---|---|
| schema | 固定 `sent-content.v1` |
| chat_id、thread_id、message_id | 目标 chat、可空话题、目标消息 |
| producer、source_kind、source_ref | 生产者、源种类、源范围 |
| source_message_ids | 全部来源消息 ID |
| url、content | 来源/内容定位与已投递正文或 caption |
| content_hash | content 原始 UTF-8 字节的 SHA-256 |
| sent_at | 带时区时间 |
| delivery_state | 旧格式仅接收 `confirmed` |
| content_id | 可选稳定内容身份 |

导出只有实际确认的目标部件能进入此格式。suppressed、ambiguous 和失败另走新系统状态导出，不能为兼容伪造成 confirmed。

### caption 镜像封装

旧封装字段为 `schema=chatdaily.sent-content-mirror.v1`、`source`、`fetched_at`、`rows_sha256`、`rows`。行内采用上表。旧哈希算法为：rows 以 JSON 序列化，键排序，保留 Unicode，分隔符逗号和冒号无空格，然后对 UTF-8 字节计算 SHA-256；数组顺序参与哈希。合成样例见 [examples.json](contracts/examples.json)。

旧导入范围只接收 x_monitor、macrumors 生产者；新 adapter 可扩充，但必须有来源身份和映射测试。旧配置的时效与大小上限放在 [兼容 profile](08-migration.md#compatibility-profile)。不可变的旧确认行不能被一次同步删除或改写。新协议可增加 epoch、sequence、schema version 与墓碑；合法清理或来源重置必须有显式 generation 切换与审计，不能默许“新文件更短所以照收”。

两类账本独立拥有权威写源。内存缓存和镜像支持原子替换、追加与外部变化可见性，不能只按文件名或 mtime 判定永远不变。

## Podcast4Bot 材料

兼容导入识别视频/文章 metadata 及其对应的字幕或文本文件。按 metadata key 关联同 basename 的 `.srt`/`.txt`，再按规范 URL、producer、内容 ID 与媒体映射核对。专栏材料可来自 article 类型，而旧 ledger producer 仍为 bilibili。

字幕非单调或损坏时可使用同条材料的文本替代，并标明退化来源。有效 metadata_description 可以作为 metadata 文档，未匹配投递账本但来源可验证的材料保留为 source_only，均不冒充原文或已送达。没有任何可用内容、身份冲突或无法验证来源时隔离，不以相似标题强行关联。本系统消费缓存与投递映射，不在此范围内替换播客生成服务。

## 模型能力接口

按用途声明 summary、verification、vision、embedding、rerank、same-event judge、growth-author、growth-judge。不同用途可共用供应商；每次调用记录用途、model/version、prompt/schema 版本、耗时、token 或可得用量、失败类型及 fallback。

接入合同包括超时、每轮/每日预算、重试分类、取消、上下文上限和输出校验。reasoning effort 是适配器可选能力；模型不支持时明确拒绝该配置或使用声明的映射，不能把未知字段当已生效。返回 200 但结构错误按失败处理。

embedding 空间必须绑定模型、维度、tokenizer、前缀/归一化及分块版本。换模型触发新代际与评测。模型冷启动和共享计算资源用有界租约；实时投递优先于后台回填，资源释放失败可诊断。模型目录列出某个名字只说明可发现，接入验证还需要实际请求与输出校验。

## 路由、健康及监控

路由适配器提供版本化的业务键到目标映射；旧 JSON 文件可以继续作为一个配置提供者，也可以由新配置服务替代。迁移期只允许一个来源负责写路由，其余读副本。

健康适配器输出数据类型、值、单位、测量时间、来源、质量与缺口。缺字段时返回缺失，不使用零值替代。监控适配器只接受脱敏的结构化 run/incident 事件，发送失败不改变业务送达状态。

<a id="network"></a>
## 网络与限流

Bilibili API 与封面直连，客户端禁止继承任意全局代理；YouTube、Telegram 和模型服务采用目标专属出口配置。进程入口清理不受支持的 `ALL_PROXY/all_proxy`，子进程继承已确定的网络策略。局域网模型端点与国外平台不能共用未经区分的客户端配置。

每目标有连接/读写超时、并发上限、最大响应大小和重试预算。429、B站 -352 等触发有界退避或暂停相应来源；不得轮换身份绕过平台限流。抓取 URL 和附件下载限制协议、重定向、大小和目的地址，避免聊天内容诱导读取本机文件或内网管理接口；已配置的内部模型端点使用独立可信配置。
