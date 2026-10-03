# 数据迁移、接管与回滚

## 独立重写需要的交接材料

实现者只依据本规格开发。迁移操作者另提供经授权的源数据导出：格式版本、采集时间、源主机/写入者身份、文件清单/hash、数据库 schema 版本、运行配置的脱敏值、接管范围及缺失项。密钥通过独立秘密配置通道提供。

下文路径均相对 `LEGACY_DATA_DIR`，属于旧数据输入合同，不要求新系统继续使用这些路径。完整 schema 描述见 [legacy-schema.json](contracts/legacy-schema.json)，它记录字段和约束，不包含业务实现。

## 数据分类与映射

| 旧输入 | 新系统承接方式 |
|---|---|
| archive 下原文、summary/concise、核验、媒体与切片 | 保留原始 bytes、相对位置和 hash；登记为内容修订及来源，派生视图可另生成 |
| chat-daily.db 的 permanent/hot_leads/repeat_topics | 保留 ID、类别、时间、状态、计数及来源；导入幂等，不能在导入时再增加 mention_count |
| growth_segments/growth_ab_log/growth_mined_days | 保留片段、引用、卡片、rubric 版本、队列和已处理日期；旧 sending 不直接重新发 |
| raw_channel_seen.txt、bilibili_seen.txt、youtube_seen.txt | 先作为 legacy_processed 证据导入，再结合 journal/回执分类；不能全标 confirmed |
| `*.holes` 和 seen overflow | 合并未完成空洞与备用记录，检测断尾/冲突；恢复源范围，不用最大 ID 覆盖所有历史 |
| dedup_journal.jsonl | 不改写历史决定；区分内容抑制、report 假设、annotate 和 delivery ambiguous |
| media_sent_ledger.jsonl / sent_content_ledger.jsonl | 按 [账本合同](04-integrations.md#ledgers) 导入；保留实际目标、多条映射及证据来源 |
| .persisted/.card-sent/.health-card-sent/.digest-sent/.run-complete/.text-push-state.json | 记录原标记及解析结果，映射对应阶段；只有 hash/sent 数量时承认缺少完整 message IDs |
| growth/weekly-*.sent | 解析 delivered/ambiguous 及 IDs，按 ISO 周映射；文件存在本身不等于确认送达 |
| growth/segments、rubric.md、rubric-history、feedback-inbox、feedback-processed-*.jsonl、getupdates-offset | 保留原话、已采用规则、历史版本、未消费与已消费反馈；已消费历史与 rubric/周报 marker 对账；offset 按当前消费者所有权交接 |
| content_seen.db | 可导入有效期内判定特征；仍保留原始数据库快照，不能用指纹补造送达回执 |
| delivered_index.db、evidence/knowledge 索引、CURRENT/PREVIOUS、release、评测/校准回执 | 保留冻结快照及评测；新索引可重建，旧向量和批准状态不得跨模型代际复用；旧数据缺少访问范围时先设为仅导入操作者可见，不能默认为共享 |
| intent-feedback 事件、主题视图与重分类记录 | 原事件幂等导入，视图重建；pending 文档关联保持 pending |
| ai91shop-codex-monitor.json | 导入已有基线/提醒去重状态；无法解释则保持人工核验范围，不重播全史 |
| 配置、路由、调度、模型预算与告警恢复状态 | 脱敏导出、逐字段说明有效值与不支持项，凭据重新绑定；配额与未结束事件连续 |
| 老 permanent.jsonl/repeat_topics.jsonl/hot-leads | 仅在存在且未并入新库时处理；按来源和 ID 对账，不能双重导入 |

## 旧状态不确定性

旧 seen 可能来自成功投递、规则终态或发送歧义。旧完成标记也可能在不同版本中采用不同语义。无法关联到完整回执的记录保留 `legacy_processed + evidence_level`，进入迁移复核范围；暂时阻止自动重播，但不计入新 confirmed 指标。

导入清单对每个不确定状态写明原因、旧证据、接管策略和可恢复操作。原始文件始终保留。被删除的源消息或失效私有媒体无法保证重建，须给出缺失清单与已有归档能恢复的范围。

<a id="compatibility-profile"></a>
## 初始兼容 profile

这些值用于复现现有部署的策略起点，属于迁移配置。它们不是新系统所有部署的固定限制。

| 项目 | 兼容要求 |
|---|---|
| 业务日与周 | Asia/Shanghai；日报上一日；周报 ISO 周。未来 profile 可改时区，需日期边界测试 |
| vision | 现有策略 `min_include_score=0.8`、`fallback_min_score=0.65`；同模型/量表先保持，替换后重评 |
| 同事件去重 | 保持 report、自动 enforce 关闭；现有 Jev 主判定与原 LLM fallback 可作为过渡接入，品牌不成为领域合同 |
| L2 人工批准 | 至少 200 条有效人工确认是最低必要条件，还需对应版本质量、覆盖、时效与明确发布决定；样本不足不得自动升级 |
| X Monitor 外部事件规则 | 原侧至少 20 条有效复核、误判率不高于 2% 是外部历史门；本系统不擅自改变其开关，也不与 L2 样本数混算 |
| mirror | 初始有效期上限 24 小时、单快照 64 MiB；允许明确改为新协议，先做资源/时效评估 |
| YouTube | 初始已知时长 ≤180 秒过滤；未知时长保留，标题显式 #shorts 则过滤。该规则是产品筛选，不作为平台 Shorts 分类的官方定义 |
| Bilibili 专栏 | 每个作者单独启用；不因已订阅视频自动扩大范围 |
| 任务与写入者 | 导入每台机器的实际计划和所有权，保留错过触发/抖动/间隔；不把仓库模板推断为现场排期 |
| 外部消费者 | 初期维持媒体账本与正文账本各自权威写源及兼容导出；接管前逐个确认消费方 |

Jev 配额、模型名称、endpoint、源清单、路由、机器和时刻通过现场导出，不能从旧 README 抄成新默认值。模型、配置和索引代际改变时，旧校准回执只保留为历史。

## 迁移步骤

1. **冻结接管范围**：逐管线列 source、route、任务、writer、启用功能和外部消费者；选择变更窗口，保留运行中的任务信息。
2. **取得一致快照**：活跃 SQLite 使用在线备份或等价一致快照，不能只复制 `.db` 而遗漏 WAL。JSONL 按完整记录边界复制并保留原文件 hash。参见 [SQLite Backup API](https://sqlite.org/backup.html)。
3. **隔离导入**：只写新目录/数据库，解析格式和 schema 版本，生成成功/隔离/缺失/冲突计数。未知字段保留在原始附属记录，不能静默丢弃。
4. **对账并重复导入**：实体 ID、正文 hash、来源关系、相册成员、目标回执、过滤/歧义状态、配额、反馈消费和源游标均比较；第二次导入结果不再增加记录或计数。
5. **影子运行**：新系统使用导出输入或只读采集，禁止向生产发送；比较纳入/排除、归档、摘要、模型预算、性能和恢复。差异分为预期改进、错误或待决定，不能只看文本相似度。
6. **测试目标实发**：使用独立 Bot/聊天验证格式、相册、长正文、权限、失败恢复和回执。生产的来源不需要因此扩大暴露范围。
7. **切换单写者**：停止或隔离旧投递执行者，处理/隔离在途 sending，采集最后增量快照；分配新 epoch/fencing，并在发送出口或旧执行环境阻断旧写入者。若外部出口无法实施 fencing，先确认旧执行环境已停止且不能自行恢复，再开放新发送；不能只靠新数据库拒绝旧提交。验证旧执行者即使恢复也不能向该目标继续发，再打开新生产路由。
8. **观察并保留恢复能力**：至少覆盖该 profile 的每一种启用调度周期，包含定时成功、一次受控失败及恢复；周任务可先演练，但正式验收还需明确记录实际周周期观察情况。

## 回滚

回滚对象包括程序、配置、计划和业务事实。新系统运行期间新增的 confirmed/suppressed/ambiguous、来源覆盖、配额、反馈消费、规则版本及用户命令先做增量导出，再合并到恢复侧。

恢复旧程序前关闭新写入者，等在途请求达到可解释状态；无法确定的请求保留 ambiguous。旧程序若不能表达新状态，使用兼容适配或暂停相应管线并人工对账，不能仅恢复切换前的旧库。外部消息无法随本地回滚撤回，继续保留其回执。

演练包含“新系统发出一条并消费一条反馈后回滚”，检查旧系统不会重发、重复消费或重复计配额。回滚包必须带清单、校验和、导入结果与写入者切换记录。

## 旧系统退役

接管门通过、回滚窗口与数据保留期形成记录后，再停止旧调度和兼容导出。原始证据、失败记录、人工标签、冻结评测及许可证保留；可重建缓存按保留政策清理。新文档的安装、配置字段和命令示例由新代码合同生成，实际运行版本通过诊断查询。
