# 去重运行指南

本文说明当前去重实现的有效规则、状态入口和复核命令。审计样本见 [审计快照](dedup-policy-audit-2026-09-23.md)，实测结果见 [实现记录](notes/dedup-policy-implementation-2026-09-23.md)。

## 运行链路

频道卡先经过 L1 ContentSeenStore，再由 L2 TopicDedupGate 对已投递内容做候选检索。L2 的字段和默认值见 [config.py](../src/chat_daily_tg/config.py) 的 DedupTopic；检索、覆盖率与模式降级见 [topic_dedup.py](../src/chat_daily_tg/topic_dedup.py) 的 TopicDedupGate。

以 mode: report、enforce_enabled: false 运行时会记录原本可能执行的动作，实际仍投递。annotate 会保留卡片并附带同事件前文链接。enforce 只有在 reranker、embedding coverage、校准回执和人工复核都满足条件时才会真正抑制。

同一事件的判定遵循以下顺序：

- 没有可靠 Jev 或 LLM 判定时只交付或标注，不执行 skip。
- new_info=none 才可能在 enforce 中 skip。
- new_info=minor 保留当前卡并附前文链接。
- new_info=substantial 保留当前卡。
- 带图卡片不能只凭 caption 判定为重复；最多 annotate。
- skip 决定无法写入 journal 时返回 deliver。

新增 L2 终态抑制在 journal 成功写入后才推进 seen。channels resend 是人工复核后的补发入口。

## X caption 镜像

BWG 的确认送达记录通过 [sync_xmonitor_sent_content.py](../scripts/sync_xmonitor_sent_content.py) 拉取为快照。快照校验由 [sent_content_mirror.py](../src/chat_daily_tg/sent_content_mirror.py) 完成，默认 24 小时后失效；失败、缩水、重写和过期都保留 fail-open 行为。

首次建立或检查快照：

    uv run python scripts/sync_xmonitor_sent_content.py
    uv run python scripts/sync_xmonitor_sent_content.py --check

镜像导入默认关闭。先校验快照并完成向量回填，再将 `xmonitor_ledger_enabled` 显式设为 `true`；其余字段及默认值以 [config.py](../src/chat_daily_tg/config.py) 的 `DedupTopic` 为准：

    sources:
      telegram:
        dedup:
          topic:
            xmonitor_ledger_enabled: false
            xmonitor_ledger_path: ~/chat-daily/state/xmonitor_sent_snapshot.json
            xmonitor_ledger_max_age_hours: 24

[run_channels_guarded.sh](../scripts/run_channels_guarded.sh) 获取频道锁后刷新快照；拉取失败记录退出码，频道任务继续执行。每分钟的 [run_ledger_sync_guarded.sh](../scripts/run_ledger_sync_guarded.sh) 只处理 r4s media 拉取与 Mac sent-content 推送。

`DeliveredIndex.ingest_sent_ledger` 不推进 Telegram 高水位，不更新其他 producer。已有 tg-cli 向量的行保留正文、向量及独立来源身份；镜像过期不影响这些行。镜像行规范化正文相同时只续期，正文变化或没有向量时才替换正文并重新回填。只有依赖镜像的记录受镜像有效期过滤；快照不可用时频道继续投递。

Caption 导入后需要同一 embedding generation 的文档向量才会参与检索。已有的限时回填入口 [backfill_delivered_embeddings.py](../scripts/backfill_delivered_embeddings.py) 接受快照；默认 dry-run 校验输入并报告覆盖率：

    uv run python scripts/backfill_delivered_embeddings.py --db ~/chat-daily/state/delivered_index.db --config ~/chat-daily/config.yaml --sent-ledger ~/chat-daily/state/xmonitor_sent_snapshot.json

维护时使用 SQLite backup API 保存一致备份，避开频道任务，并确认配置指向的 embedding 服务健康、认证环境变量已加载。追加 `--apply` 后，命令在 `--max-rows`、`--max-seconds` 的预算内导入确认 caption 并生成向量，不发送消息或推进 Telegram 高水位。回填不负责启动模型服务；本机按需服务生命周期由 [qwen_runtime.py](../src/chat_daily_tg/qwen_runtime.py) 的 `channel_runtime` 管理。后续频道任务根据覆盖率、generation 和校准状态决定 `effective_mode`；回填成功不会自动升级模式。

## X Monitor 的折叠补丁

BWG 侧的纯翻译折叠、同锚点官方引用折叠和官方自回复线程合并已整理为 [x-monitor-dedup-implementation.patch](notes/x-monitor-dedup-implementation.patch)。该补丁针对本次审计读取的 BWG 源码快照；本机旧版 x_monitor 与该基线不一致，因此没有用整文件覆盖旧 checkout。补丁包含 quote_fold.py、thread_merge.py、twitter_monitor.py 的接线和对应回归测试，应用前先核对 BWG checkout 的基线。

X 侧开关默认关闭，部署配置分阶段启用：先 `translation_reply_enabled`，观察至少两天已送达翻译的回复结果，再启用 `official_quote_groups`，最后启用 `official_thread_merge_enabled`。事件账本继续 `observe`。部署包中的 `config.fragment.json` 只启用第一阶段的翻译回复；完整片段单独保存，应用前逐项合并。

账号数组按顺序处理。产品官号须位于其开发者账号之前：`claudeai` → `ClaudeDevs`；`OpenAI` → `OpenAIDevs` → `thsottiaux`。部署包的 `reorder_twitter_accounts.py` 默认预览，`--apply --backup` 在保留每个账号对象和其他账号位置的前提下写入该顺序，并保存备份。原推尚未 confirmed 时 Quote 独立投递；同轮稍后确认原推不会追溯改写已经发送的卡片。

`translation_reply_enabled` 开启后，同目标话题内已有 confirmed 原文的纯翻译回复原卡。官方同锚点引用没有新增事实且没有自有媒体时，写入 journal 后终态折叠；有增量时回复原卡。来源不明、目标不匹配或元数据损坏时独立投递。

`thread_merge.merge_ready` 合并带 `push_policy` 的同 conversation 连续自回复文本。仅 `dev_release`、`model_api`、`model_launch`、`major_product_launch` 进入 90 秒 idle 等待；额度恢复、补偿、赠送、计划权益和模型访问消息立即处理。媒体、引用和 Article 保持独立路径，带图首帖不会参与线程合并。等待或发送失败的所有成员保存在 retry，成功后才统一记录 seen。retry 在下一次 cron 运行恢复，实际等待取决于轮询间隔。

部署前校验基线和备份受影响文件，确认没有运行中的 `twitter_monitor.py`，再选择 cron 两次启动之间的空档。`:05–:25` 只是候选窗口，仍须检查进程。部署后先执行只预览的 `run.sh --dry-run` 核对开关加载与折叠计划，再由原有 cron 执行。补丁、配置重排与回退命令见交付包 README。

## 生产复核

事件账本只观察不抑制时，查看候选和门槛：

    python /path/to/x_monitor/event_ledger_review.py list
    python /path/to/x_monitor/event_ledger_review.py report

逐条检查 X 原文后再标注：

    python /path/to/x_monitor/event_ledger_review.py label ID --valid-suppression --note "同事件且无新增事实"
    python /path/to/x_monitor/event_ledger_review.py label ID --false-positive --note "存在新的结构化事实"

X 事件账本的门槛为至少 20 条人工复核、误判率不高于 2%；Jev L2 抑制继续要求至少 200 条人工确认样本。日期快照与候选数量见 [实现记录](notes/dedup-policy-implementation-2026-09-23.md)。

## 夜间频道轮询

频道排期事实源是 [schedule.yaml](../schedule.yaml)。查看模板和已装 plist 的差异：

    uv run python scripts/schedule.py list

修改时间后先运行 apply --dry-run，再执行 apply；运行中的 label 会跳过重载。wrapper 的 jitter、锁和 write-after-send 语义保持不变。
