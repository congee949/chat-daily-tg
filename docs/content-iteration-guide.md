# 内容回放、反馈与候选规则

本指南说明现有项目上的增量工具。设计与验收条件见
[迭代设计](design/2026-09-29-content-iteration.md)，本地实测与待验收项见
[实施记录](process/content-iteration-2026-09-29.md)。真实样本放在受限工作目录，避免提交聊天原文。

## 本地命令

统一入口是 [content_iteration_cli.main](../src/chat_daily_tg/content_iteration_cli.py)。
每个命令读取一个 JSON 请求并输出 JSON；`--root` 明确指定本地状态目录。
请求字段与对应函数参数一致，错误会以退出码 2 返回。

```bash
uv run python -m chat_daily_tg.content_iteration_cli --help
uv run python -m chat_daily_tg.content_iteration_cli freeze \
  --request work/content-iteration-2026-09-29/label-request.json \
  --root work/content-review --output work/content-review/samples-v1.json
```

`freeze` 请求包含 `version`、`split_version` 和 `samples`。样本字段及标签枚举由
[content_replay.freeze](../src/chat_daily_tg/content_replay.py) 校验。每次正文更正或标签修订使用新版本、
新输出路径；同一路径只能重复校验完全相同的冻结内容。先完成事件分组，再划分开发集和留出集。

`replay` 请求引用 `manifest`、`rules` 和 `predictions` 三个 JSON 文件路径，并指定 `split`。
`rules` 是当前、候选两条规则的数组；每条包含 `version`、`prompt_hash`、`schema_version`、`rank_version`。
`predictions` 是评测器的逐样本输出数组，每条携带 `sample_id`、`rule_version`、`input_hash`、
完整 `rule` 与 `result`；`result` 包含 `decision`（include/omit/undecided）和实际 `model`。
该命令校验已有预测并生成对比报告；模型执行器可调用 `content_replay.replay` 的 `evaluate` 接口。

留出集额外要求 `holdout_contract`，字段见 `content_replay.replay`。模型失败与人工标签分列，
保留率和可省略内容放行率同时给出标签总数、成功评测分母及失败数。缺少人工标签时比率为空。
回放函数本身只读取传入样本；注入的评测器必须遵守离线回放边界，不发送 Telegram 或修改业务状态。

## 回复反馈

[content_feedback.ReplyIntake](../src/chat_daily_tg/content_feedback.py) 仅接受 owner 在白名单 chat/topic
中回复消息的“展开”和“少推这类”。操作表记录入口状态，意图事件继续使用 `FeedbackStore`。
文本映射仅使用 confirmed 的公开频道账本，并校验 `original_messages` 的来源 ID 顺序与 hash。
`content` 保存投递处理后的文本，展开使用独立的逐消息原文。旧记录缺少原文字段时进入 `needs_match`，
相册和分段消息通过内容身份归并。
媒体还需要 `verified_original=true`、producer 与 URL 一致的原文目录记录，字段包括
`content_id`、`source_ref`、`text`、`url`、`producer`。无法唯一关联时保留 `needs_match`。

可选配置文件为 `~/chat-daily/state/content-feedback.json`，格式见
[禁用状态的配置示例](../examples/content-feedback.json)。配置 owner 必须与现有成长消费者一致。
直接轮询分支在持久化入口事件后推进 offset。启用 reply_intake 时，relay 分支使用 `feedback_relay_reader.READER_SCRIPT` 通过 SSH 只读查询既有消费者状态库，
保留 owner 私聊文本，并返回白名单 chat/topic 中的完整回复信封。远端消费者须已有 messages.payload_json 原始消息列。
不另起 `getUpdates` 消费者，不在远端安装文件；未启用回复入口时继续使用原私聊 relay 命令。

回复发送前持久化 `response_unknown`。只有取得消息 ID 才改为 `responded`，异常或进程中断保留待复核状态。
“少推”进入每周候选输入，带原文和来源，不修改信源开关。`summary()` 输出各处理状态数量，
`DeliveryReview.import_replies` 将未知回复导入复核库。

## rubric 候选

成长周报的 `merge_rubric` 只生成候选，在线 rubric 保持原版本。
[RubricCandidates](../src/chat_daily_tg/rubric_candidates.py) 拥有状态转换、父版本检查与回滚规则。

| 命令 | JSON 请求字段 |
| --- | --- |
| `rubric-draft` | `parent_path`、`candidate_path`、`feedback_ids`、`reason` |
| `rubric-evaluate` | `identity`、`manifest` 文件路径、`report` 文件路径 |
| `rubric-review` | `identity`、`actor`、`decision`（approve/reject）、`reason`、`explanations`（样本 ID 到理由） |
| `rubric-activate` | `identity`、`active_path` |
| `rubric-rollback` | `active_path`、`actor`、`reason` |

评测必须绑定父规则与候选正文 hash，并完整覆盖所选划分；成长回归要求该划分内至少 20 条已标注样本，
另有长度、原意、夸大标签。值得推却被候选省略的每条样本都需要人工解释。
候选保存完整分歧文件与优先复审条目。切换前核对 active 的父版本，切换前保存回滚内容。
周报最多展示五个最近候选的状态、增删行数、本地 `.diff` 与候选文件路径；
评测文件 hash 通过校验后才展示其入口。候选生成不改变周报中的生效版本。

## 价值排序和来源质量

`rank` 读取候选原文、逐条 profile 和策略版本，仅输出预览列表。
字段和类型权重见 [value_profiles.FIELDS / POLICIES](../src/chat_daily_tg/value_profiles.py)。
可判定项必须引用原文片段和相同来源；结构错误时保留完整原排序。
`blind` 输出两个匿名列表和单独的 `private_key`，展示给复审者前移除该键。
人工记录有用条数、必读漏选、类型覆盖和阅读耗时；额外模型调用数从执行回执统计。
`source-quality` 分别统计采集结果、人工有用率分母及确认/未知出处，不合成单一信源分数。

## 事件档案与投递复核

`event-create/propose/decide/rebuild` 对应 [EventFiles](../src/chat_daily_tg/event_files.py) 同名操作。
`create` 需要 title 和关注者 actor；`propose` 保存原文及关联理由；`decide` 由人工确认或撤销。
同主题新事件不能确认进入旧事件。档案事实引用原文精确片段，更正保存原始版本，撤销归属后可重建。
来源组只有经人工确认才计入独立出处，未知来源单列。

`review-add/decide/report` 对应 [DeliveryReview](../src/chat_daily_tg/content_operations.py)。
确认送达需 Telegram 消息引用；确认未送达需核对范围和操作者。
`retry_requested` 只记录明确重发请求，执行重发仍须连接对应生产者的原有状态链路。
成长 ambiguous 记录与反馈未知回复分别用 `import_growth`、`import_replies` 只读导入。

`health` 读取机器本地观测，不用同步副本推断远端健康。输入必须带机器、生产者、证据和后续操作。
缺少下一次观测不会自动认定恢复；恢复需要明确事件。当前工具生成本地 JSON，尚未接入定时通知。

## 知识检索任务

检索发布要求沿用 [KnowledgeIndex 操作指南](knowledge-index-runbook.md)。
新 `task` 命令通过既有正式或 diagnostic handler 执行，不更改发布指针。

```bash
uv run chat-daily-knowledge task --help
uv run chat-daily-knowledge task recall '需要找回的主题' \
  --diagnostic --generation GENERATION_ID \
  --feedback /absolute/private/events.jsonl \
  --delivered-ledger /absolute/private/sent_content_ledger.jsonl
```

`recall` 优先使用明确 read 事件；完全没有 read 事件时查已投递内容。
`progress` 限定最近七天。最多返回三条，并带日期、原文、片段和范围；`--expand-archive` 显式扩大来源。
范围限制在精确、全文与向量检索各自截取候选之前应用；`--top-k` 控制范围内返回数量。
通过 `--event-root` 指定事件档案目录后，结果按 content_id 关联已确认归属且存在 Markdown 档案的事件。
20 条真实找回问题的体验评测需独立记录。

## 调用审计

`calls-report` 请求可传 `stale_after_seconds`，空对象使用代码默认值。
`--root` 指向对应机器的 `jev-calls` 目录，`--output` 指向当次报告。
统计字段及中断判定见 [CallReceipts.report](../src/chat_daily_tg/call_receipts.py)，
原始响应和复用规则见 [Jev 指南](jev-dedup.md#调用回执与响应复用)。
`unknown_attempts` 同时保留未知结果和缺少可靠终态的调用；年龄只决定是否需要复核，不授权重试。


`calls-export` 请求为 `{"attempt_id":"实际 attempt ID"}`，按单次调用导出 JSON 响应。
导出先验证原始字节 hash，再逐字段脱敏凭据并记录导出 hash；原始归档保持不变。
非 JSON 或 hash 不匹配的响应会拒绝导出。自由文本仍需核对个人信息，产物显式标记
`privacy_review_required=true`，完成复审后再纳入可分享回放材料。

## 每日复核文件

`daily-review` 从明确指定的本机源导入未知投递，生成按北京时间日期组织的 Markdown 和 JSON 快照，
`latest.json` 指向最近结果。请求示例：

```json
{
  "machine": "当前执行机器标识",
  "sources": [
    {
      "kind": "growth",
      "machine": "当前执行机器标识",
      "authority": "local",
      "path": "/absolute/private/chat-daily.db",
      "target": {"chat_id": "当前配置目标，仅用于核对"}
    }
  ],
  "observations": []
}
```

`kind=replies` 读取反馈操作库，不需要另传 target。每个输入只读打开，失败单独写入报告，
其余源继续导入；同一 attempt 重复导入保持原复核状态。健康发现使用 `observations` 提供的本机证据。
成长历史记录不保存实际发送目标，因此 target 只作为配置提示，标记需核实历史目标，不能代替送达证据。
该入口不发送消息、不重发、不修改原始源库；调度和通知仍需单独接入。

### 事件关联建议

`event-suggest` 请求包含 `config`（配置路径）、`model_alias`（已有模型别名）、`key`（事件 ID）
和 `source`。`source` 包含 content_id、text、url、publisher、published_at。
命令调用现有 `SameEventJudge`，最多使用最近三条人工确认的事件来源作为上下文。
没有确认来源时返回 `needs_seed_review`，不调用模型；已有人工确认或拒绝时返回原决定。

同事件结果只进入 proposed。非同事件结果保持 undetermined，因为该裁判不判断是否属于同一主题。
每次建议保留独立回执、输入与上下文 hash、模型身份和结果，模型失败不改变任何既有归属或投递状态。
实际网络调用使用所选配置的超时和重试；命令不自动确认建议。

### 生成价值排序预览

`value-preview` 请求包含 `config`、`model_alias`、`task`、`candidates`，可选 `selection_count` 和 `policy`。
候选逐条提供 content_id、text、source_ref。命令调用指定模型生成带原文片段的 profile，
由 `rank_candidates` 校验引用并计算排序，输出 original 和 candidate 两份完整列表。
第二次评审只覆盖入选集合分歧项和截断位置两侧条目。任一条结构或引用无效时保留原排序。

产物绑定提示词、schema、排序策略 hash、关注任务和输入身份，记录每次评审阶段、结果、usage 与耗时。
`assessment_calls` 表示逻辑评审调用；只有每次 client 都返回有效 metrics 时才汇总 `network_attempts`，
避免把含重试的调用当成一次 HTTP 请求。当前入口生成本地候选池预览，尚未接入日报的自动候选提取。

### 保存盲评与人工结果

`blind-prepare` 接受 `current`、`candidate`（同一完整候选池的两种顺序）、`evaluation_id`、
`extra_model_calls`（实际额外评审次数）和 `contract`。
contract 必须在评测前明确 selection_count=10、severe_miss_definition、adoption_criteria。
输出 review.json 与 private.json 两份冻结文件。只把 review.json 提供给复审者；
private.json 保存版本对应关系与完整候选池身份。换版本使用新的 root，旧文件不覆盖。

`blind-result` 的请求引用 review、private 文件路径，另提供 actor、choice（A/B/tie/neither）、reason、
reading_seconds（A/B 分别实测的秒数）和 labels。每个候选的人工标签包含 content_id、useful、must_read、
content_type（tool/practical/research/news/other）和 reason。必须标注完整候选池才能计算必读漏选。
结果保留原标签，分别列出有用条数、必读漏选 ID、类型覆盖、耗时和额外调用数。
人工选择与候选采用是两个动作，统计报告不自动切换排序。

### 执行模型回放

`model-replay` 请求引用 manifest、rules 文件，并指定 config、model_alias、split，可选 holdout_contract。
rules 中每条规则还需提供 text，prompt_hash 必须等于该正文的规范 JSON hash（`call_receipts.digest`）。
留出条件在调用模型前校验。每次运行创建独立目录，逐条保存原始响应、回执及最终报告；无效 JSON、
缺少理由或引用不属于原文时记录失败。回放不发送 Telegram，也不写 seen 或投递完成 marker。

原始响应文件可能包含聊天内容，仅供本地复审。`model-replay` 的实际调用使用现有配置的超时和重试，
运行 ID、模型身份、评测器提示词 hash 与规则身份一并保留，人工质量标签仍需独立填写。

### 更正与来源完整性

事件来源在写入和读取时校验日期、URL、逐字事实引用及正文 hash；来源 URL 不允许嵌入凭据。
正文变化产生新版本。correction_of 必须指向档案中已有且不同的版本，旧原文始终保留。
无效事实片段在写入前拒绝，避免先确认后生成失败。直接修改已归档正文会导致身份校验失败。

### 健康通知

`health-notify` 请求为 `{"health":"本地 health JSON 文件路径"}`，文件格式与 `health` 命令输出一致。
命令复用 `incident_client.report_warning/report_recovery` 的独立 controller 配置；未配置或未取得 accepted
回执时保留 pending，不回退为无幂等保障的 Telegram 重发。

首次发现生成固定 source_event_id，控制器接收后，相同问题的后续巡检只合并本地次数。
恢复沿用该 ID，失败恢复记录会跨运行重试，即使下一份巡检报告已不再包含它。
通知状态保存在 root/health-notifications.json，正文投递与 seen 不受影响。
controller accepted 表示控制器接收，实际通知是否投递仍需核对控制器与 Telegram 回执。

### 本机健康采集

`health-collect` 请求包含 machine（必须等于执行主机的 hostname），可选 growth_db、jev_journal、
journal_max_age_seconds。工具只读源库/日志，输出 observations 与读取记录；将 observations 传给
`health` 或 `daily-review` 后生成汇总。

成长观测只检查 sending 租约是否过期或缺少截止时间，不把 pending 库存当成故障，不执行重发。
Jev 观测依据最新、完整且在新鲜度窗口内的 judge 回执；降级状态与健康输入错误分开记录。
缺失、截断、时间非法或过旧的日志都不能形成恢复证据。过旧记录输出 stale_judge_receipt、
末次回执时间与当时状态；没有近期调用可能是没有候选需要裁判，须结合任务日志核对，不能据此判断模型故障。当前采集器尚未覆盖微信、Telegram、B站、YouTube
的成功抓取时间与连续空结果，这些观测仍需接入各生产者。

### 信源抓取回执

微信 `export_group` 在输出归档旁追加 fetch_health.jsonl，记录已验证的源消息数量、开始/结束时间及
success/no_update/parsed_empty/failed 状态。末条内容时间无法取得时为空，不用归档时间代替。
回执写入失败仅记录错误类型，原导出结果与异常保持原样。

`fetch-health` 请求包含 machine（本机 hostname）、journals（本机回执路径数组），可选 empty_threshold。
工具去重 attempt，按 producer 与 source_ref 分组，输出抓取失败、解析为空、连续零结果的独立观测。
连续零结果需要核对是否确实无更新，不直接判断抓取故障。输入截断或读取失败时不生成恢复结论。
输出 observations 可传入 health/daily-review；input_failures 必须同时保留供复核。
B站视频 API（bilibili-video-api）和 YouTube RSS（youtube-rss）已按 UP/频道写入该格式，
路径为 seen 文件同目录下的 fetch_health.jsonl。源数量在去重和时间过滤前统计，全部已见不会记成 no_update。
B站专栏 API、opencli 列表和 YouTube Data API 后备已追加对应通道回执。
专栏分页中途失败保留 failed 与此前读取数量；RSS 失败和 Data API 成功分别保留，不覆盖前者。
opencli 回执目前描述列表发现，不代表后续逐视频详情读取成功。Telegram 日报批量同步及旧版单聊同步已接入归档旁回执。批量返回 synced 按 synced_messages 口径保存；
单聊缺少可靠数量时记 sync_completed，count 留空，不能据此累计零结果。
日报同步失败后继续读缓存时保留失败回执。公开频道增量回填已分别记录 telegram-channel-sync 与 telegram-local-window。
前者只确认同步命令完成，后者记录指定时间/HWM 窗口的本地行数与可解析的最新消息时间；
本地窗口为空不能证明整个远端频道无更新。私有媒体单次/批量抓取已记录 telegram-media-metadata，媒体实际下载另记 telegram-media-download。
未请求和大小限制过滤不算下载失败；旧 manifest 缺少 download_status 时不推断下载成功。
回执写在下载目录的父目录，保留在媒体清理之后。

### 复核反馈回复

`reply-review` 的 root 指向原反馈入口目录，请求包含 intake（原 bot_id、owner_id、targets、text_ledger 等配置）、
key（bot:update 操作身份）、decision、actor 和 evidence。evidence.chat_id 必须匹配原目标。
confirmed_sent 需要 message_ids 与 telegram_reference；confirmed_absent 需要 checked_scope。
确认未送达后停在 response_absent，普通 drain 不会重发。
只有随后显式提交 retry_requested 并填写 reason 才重新开放处理；新发送生成独立 attempt，反馈事件沿用原幂等键。
确认送达后补写回复回执，不重复发送。每日复核导入按发送 attempt 区分同一反馈的不同尝试。
该命令修改本地入口状态，不自行发送消息；下次已有消费者 drain 会处理获准重试的条目。

### 成长卡片复核

`growth-review` 请求提供 db_path、seg_id、expected_sent_at（原 ambiguous 发送时间）、decision、actor 和 evidence。
evidence 需要人工核对的历史 chat_id；确认送达还需 message_ids、telegram_reference，确认未送达需 checked_scope。
只有已有 confirmed_absent 决定后，才能提交带 reason 的 retry_requested，且目标须与该次核对一致。

复核以事务保存旧片段快照与决定，检查原状态和原发送时间。确认送达保留 sent，确认未送达继续停在原状态；
显式重试才改回 pending，让原成长任务重新领取。该命令不调用 Telegram，后续任务仍使用其当时有效的路由和生成逻辑，
因此执行重试前需核对当前目标配置，生成的卡片文本也可能与首次尝试不同。旧 attempt 与原正文证据保留在复核表中。

每日复核导入会同步原入口的复核历史：成长以 segment_id 与原发送时间定位尝试，回复使用发送 attempt ID。
旧尝试的 confirmed_absent/retry_requested 与新尝试的成功回执分别保留；重复导入不回退已有终态。
回复入口已保存明确 message IDs 的 responded 状态可投影为 confirmed_sent；未取得回执时仍保持 unknown。

检索任务可接受知识来源解析器已确认的 source_content_id 映射，要求 mapping_status=confirmed 且 confirmed=true。
映射 pending 的 read 事件不用于匹配，也不触发自动回退到已投递范围；响应给出 unresolved_read_events。
只有完全没有 read 事件才默认使用已投递范围，扩大到归档仍需显式 expand_archive。
同一 content_id 的多个命中片段保留最高排序的一条，最多三个不同内容。程序不以相似标题或 URL 猜测身份。

### 按时间窗口复盘来源

`source-weekly` 请求包含 journals（抓取回执文件数组）、samples（带原始来源与人工标签的样本数组）、
start/end（带时区，结束时刻不包含），可选 markdown_path。producer/source_ref 必须使用一致的来源身份，
工具不把标题相似或 URL 不同的来源自行合并。

报告按来源分别给出抓取成功数/尝试数、人工值得推数/有效标签数、暂无法判断与未标注数量、确认出处组与未知条目。
no_update 是成功抓取，parsed_empty 单列；无标签时有用率为空。
重复 attempt/sample 身份去重，输入读取失败时报告标明 partial 并保留失败来源。JSON 与 Markdown 由同一统计结果生成。

### 从日报归档预览排序

`daily-candidates` 请求指定 archive_dir，只读 concise.md 与同目录微信/Telegram 原文归档。
以简报一级列表条目为候选，仅当其中链接唯一对应一个完整原文消息块时建立映射；
返回原文、字符区间、行号、归档 hash 和原候选输出。缺少链接或多来源匹配单列 unmatched。

`daily-preview` 另需 config、model_alias、task，可选 selection_count。它只对可唯一定位原文的条目运行
value preview，输出原顺序与候选顺序，不改 concise.md、不推送、不推进状态。零匹配时不调用模型。
该精确链接方案覆盖有限，不能用已匹配子集的结果代表整份日报质量；多来源综合条目需补显式来源身份。

多来源或没有 URL 的条目可传 bindings：包含 summary_hash 与 items；每项提供 summary_offset、actor、reason
以及 sources 数组。每个 source 指定 path、char_start、char_end、archive_hash，必须精确对应当前归档中的完整消息块。
同一候选可绑定多个原文块，输出 source_locators 保留每个位置。文件变化、摘要版本变化、重复来源或不完整区间均拒绝。
没有 bindings 的条目继续使用唯一 URL 规则；显式映射不由模型自动批准。

### 发送器统一未知回执

`TelegramSender.delivery_review_root` 或显式设置的 `CHAT_DAILY_DELIVERY_REVIEW_ROOT` 可指定统一复核目录。
未配置时不写该库。正文、富消息、图片、单媒体、相册在既有 AmbiguousDeliveryError 边界追加记录，
保留目标、API 方法、请求尝试开始时间和错误类型，不保存 token、正文或带凭据 endpoint。

业务入口可传 logical_content_id 和 producer；缺少逻辑身份时记录独立 unmapped-request，content_mapping=needs_match，
人工补齐前不能将它当作原文身份。写库失败仍抛原未知结果异常，保持禁止自动重发。
该通用清单不自动修改各业务生产者状态，也不提供未经核对的通用重发按钮。

B站/YouTube digest 在每条卡片的发送作用域使用 `delivery_identity` 绑定现有 seen-key 内容身份。
封面、文字和降级路径共享该身份，作用域退出时恢复，不修改 sender 的持久属性。
开启统一复核目录后，这两个生产者的未知记录可直接定位内容；未启用目录时保持现有行为。

公开频道使用与 sent-content 账本一致的内容身份，私有媒体使用频道与首条消息 ID 的组合。
同一 delivery_identity 作用域中的成功分段/媒体消息 ID 会累积为 known_receipts，后续未知异常保留这些 ID，
用于人工区分部分送达与完全未知；退出作用域后清空，避免关联到下一条内容。

### 事件进展与关注状态

来源正文 ID/hash 变化仍更新综述版本，以保留证据绑定；另存 progress.json 记录确认事实、冲突和更正的变化。
重复的逐字事实引用不会新增事实进展，标题变化或只新增无事实来源也不计进展；撤销后移除的事实会留在历史记录。
该规则按已确认引用的精确文本比较，不自动把近义改写视作相同事实。

`event-status` 请求为 key、state（following/paused/closed）、actor、reason。状态只通过明确操作改变，
不会因长期无更新自动关闭。paused/closed 的 suggest 不调用模型；档案继续显示已有来源与最后来源时间。

`media-originals` 可从已配置的 Podcast4Bot 归档与媒体投递账本生成 originals JSONL。
请求提供 podcast_root、media_ledger、output_path。只收录明确投递关联的 article/srt/transcript，
排除 metadata_description 和未确认来源。目录中的 verified_original 仅表示归档正文与投递关联通过校验，
不表示 ASR 文本已经人工校对。设置反馈配置 originals 为该文件后，映射仍逐次验证文本 hash 和唯一性。

### 反馈试用统计

`feedback-report` 使用原入口 root，请求为 `{}`，只读 operations.sqlite3。
统计已接受的逻辑指令数、expand/filter 数、唯一匹配数、反馈记录数、带明确回执的展开成功数、
未知回复/待匹配/失败状态及实际发送 attempt 数。重复 update 不增加逻辑指令，重试只增加 attempt。
匹配率和展开成功率给出对应分母；数据库缺失返回 available=false，无样本时比率为空。
该报告不统计已读，拒绝的非 owner/错误目标更新也不在已接受指令分母中。

已读/已投递身份和最近七天时间限制传入索引查询，词法降级沿用同一范围。
显式扩大到归档仅在范围内零结果时执行，并共享该任务八秒总时限；剩余时间耗尽时返回 expansion_error，
不会把未执行的扩大查询写成已扩大。范围限制仍不替代索引覆盖与原文可达性验收。

rubric-evaluate 会逐条核对冻结样本的 content_id、正文 hash、source_ref、人工标签与完整规则身份，
并从逐条结果重算指标和分歧集合。重复结果、同名规则、汇总不一致或缺失分歧均拒绝进入 evaluated。
优先复审列表由已验证结果生成，不直接信任报告自行声明的前五条。
