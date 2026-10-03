# 去重整改实施报告（2026-09-23）

依据：[复查与整改计划](/Users/Apple/Projects/chat-daily-tg/docs/notes/dedup-review-and-remediation-2026-09-23.md)。验证环境：Mac `MateBook-Fold.local`，账号 `Apple`；源码 `/Users/Apple/Projects/chat-daily-tg`，运行数据 `/Users/Apple/chat-daily`。实测时段为 2026-09-23 21:53–22:48，时间均为 Asia/Shanghai。

## 结果

Mac 镜像覆盖向量、同步执行权限及拉取频率问题已修复，生产索引已完成向量恢复。336 小时窗口的有效向量覆盖率从 53.44% 提升到 100%；回填时 594 条 X Monitor 记录全部补齐向量，Telegram 高水位保持 9986，SQLite 完整性检查为 `ok`。

Mac 完整测试 1421 项通过；X Monitor 隔离副本完整测试 514 项通过。X 的账号顺序修正工具、idle 事件白名单及只读预览已装入可校验补丁包，BWG 尚未部署。00:00、02:00 排期继续保留，配置与已装 plist 一致。

临时冻结已在回填及重复导入验证后解除。生产配置显式启用 caption 镜像导入；代码默认仍为关闭。Jev 主裁判、原 LLM fallback、`mode: report`、`enforce_enabled: false` 保持原策略。

## 修复与行为

| 问题 | 修改后的行为 | 实现依据 |
| --- | --- | --- |
| 镜像正文与 tg-cli 文本存在格式差异，导致旧向量被清空 | 已有 tg-cli 向量的记录保留正文、规范化文本、向量及 generation；保留 `mirror_source=NULL`，镜像过期不会隐藏这些独立记录 | [topic_dedup.py](/Users/Apple/Projects/chat-daily-tg/src/chat_daily_tg/topic_dedup.py) 的 `DeliveredIndex.ingest_sent_ledger` |
| 重复导入与来源保护 | 同源镜像规范化正文相同只续期；无向量或正文确有变化时才替换并等待回填；其他 producer 在任何更新前被排除 | 同上；[索引回归测试](/Users/Apple/Projects/chat-daily-tg/tests/test_topic_dedup.py) |
| 镜像随代码更新自动开启 | `DedupTopic.xmonitor_ledger_enabled` 默认改为 `False`，运行配置须显式开启 | [config.py](/Users/Apple/Projects/chat-daily-tg/src/chat_daily_tg/config.py)、[配置测试](/Users/Apple/Projects/chat-daily-tg/tests/test_config.py) |
| sent-content 同步脚本不能执行 | 文件权限由 644 改为 755 | [sync_sent_content_ledger.sh](/Users/Apple/Projects/chat-daily-tg/scripts/sync_sent_content_ledger.sh) |
| 每分钟重复拉取 X caption | ledger-sync 仅保留 media 拉取与 sent-content 推送；X 镜像在频道 wrapper 成功持锁后、jitter 前刷新 | [ledger wrapper](/Users/Apple/Projects/chat-daily-tg/scripts/run_ledger_sync_guarded.sh)、[频道 wrapper](/Users/Apple/Projects/chat-daily-tg/scripts/run_channels_guarded.sh) |
| 镜像网络失败影响面与 SSH 参数 | 镜像失败只记录状态，频道继续；统一 BatchMode、8 秒连接超时及 accept-new 主机密钥约定，保留远端路径引用与 last-good | [sync_xmonitor_sent_content.py](/Users/Apple/Projects/chat-daily-tg/scripts/sync_xmonitor_sent_content.py) 的 `SSH_OPTS`、`main` |
| 频道失败路径丢失真实退出码 | 修复 `exit=$rc，` 被 Bash 误读为变量名的问题，改为 `exit=${rc}，` | 频道 wrapper；[隔离行为测试](/Users/Apple/Projects/chat-daily-tg/tests/test_dedup_sync_wrappers.py) |
| Quote 的原推在同轮稍后才确认 | 提供账号顺序工具，使 claudeai 先于 ClaudeDevs，OpenAI 先于 OpenAIDevs 和 thsottiaux；只交换这些账号所在槽位 | [X 包](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/README.md) 的账号重排步骤 |
| 额度类通知进入 idle 等待 | 合并及 90 秒等待仅适用于 dev_release、model_api、model_launch、major_product_launch；额度和权益类首条立即处理 | [thread_merge.py](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/thread_merge.py) 的 `IDLE_WINDOW_EVENT_TYPES`、`merge_ready` |
| X 预览启动仍恢复 pending 状态 | dry-run gate 改为 SQLite 只读，跳过 stale pending 恢复，打印加载开关及折叠计划 | [twitter_monitor.py](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/twitter_monitor.py)；真实数据库字节不变测试 |

已被旧逻辑覆盖的历史记录继续使用镜像来源身份，回填恢复其向量；未凭当前表推测或重建丢失的 tg-cli 来源。新的向量保留规则覆盖后续导入。X 带图首帖仍走独立媒体路径，不参加文本线程合并。

## 生产恢复

先显式关闭镜像自动导入，等待 22:00 的计划频道任务结束并释放模型。随后使用 SQLite backup API 生成一致备份，持有频道锁执行 sidecar。每轮最多 256 条、最多 600 秒；三个实际批次依次更新 256、256、117 条，共 629 条，耗时约 703 秒。每轮镜像导入新增或改写正文数均为 0。

| 指标 | 回填前 | 回填后 |
| --- | ---: | ---: |
| 336 小时有效窗口、非空规范化正文 | 1351 | 1351 |
| 当前 generation 有效向量 | 722 | 1351 |
| 缺失向量 | 629 | 0 |
| 不兼容／无效向量 | 0／0 | 0／0 |
| coverage | 0.534419 | 1.0 |
| X Monitor 行及非空向量 | 594／0 | 594／594 |
| Telegram 高水位 | 9986 | 9986 |

复查时的 597 条与回填时的 594 条处在不同快照：回填前备份已是 594 条。sidecar 不裁剪历史记录、不推进高水位，不发送 Telegram 消息；结束后 Qwen runtime 正常释放。22:00 计划任务日志记录两张频道卡完成投递，该轮发生在 sidecar 前，未作为新增语义效果的验收。

数据库备份位于 `/Users/Apple/chat-daily/state/delivered_index.db.bak-remediation-20260923-221303`，SHA-256 为 `407b47d94b459a3f387b87871859063c04cb0c22293631545085d6a9f48a340e`。配置冻结和恢复前均保存了本机备份，恢复前备份为 `/Users/Apple/chat-daily/config.yaml.bak-dedup-remediation-post-backfill-20260923-222900`。备份不放入交付 ZIP。

[回填结构化结果](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-backfill-results.json)、[完整回填日志](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-backfill.log)、[备份与完整性回执](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-backfill-backup.json) 保存本次证据。

回填通过后将运行配置显式恢复为 `xmonitor_ledger_enabled: true`，使后续频道轮次持续导入并续期镜像。配置解析结果见 [运行配置快照](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-config-status.json)。恢复后的下一次计划频道轮次尚未发生，新增镜像语义命中效果尚待观察。

## 验证

| 检查 | 结果与证据 |
| --- | --- |
| Mac 完整 pytest | 1421 passed，65.11 秒；[日志](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-mac-tests.log) |
| X Monitor 完整 unittest | 514 tests，0.541 秒，OK；[日志](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-x-tests.log) |
| 镜像及 wrapper 场景 | 覆盖重复导入、过期、本地来源保留、其他 producer 保护、锁冲突、同步失败继续频道、退出码保留及可执行权限；纳入完整 pytest |
| 账号顺序及 X 预览 | 覆盖原推未确认／确认后动作、原对象和其他账号位置不变、急迫事件不等待、带图首帖、重试恢复和 SQLite 不变；纳入 X 完整测试 |
| X 补丁 | 在保存的基线实际应用，6 个变更文件 SHA-256 均等于目标；[重建校验](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/patch-verification.json) |
| 排期 | YAML 与已装 plist 一致，00:00、02:00 保留；[排期核对](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-schedule.txt) |
| 静态与交付 | shell 语法、Python 编译、git diff --check 通过；124 处本地链接与锚点有效，ZIP 内 23 文件与目录相同，6 个源码哈希匹配；[交付校验](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-delivery-verification.json) |

测试中的 SSH、Telegram、guard 和 caffeinate 使用隔离替身。生产验证使用只读状态检查和不发送消息的向量维护入口。没有提交、推送 Git 或发布 BWG 代码。

## 仍待处理

1. **BWG 部署和观察。** 已交付经过修复的包。第一阶段片段只启用翻译回复；观察后再开启官方圈，线程合并最后开启。部署顺序与账号重排命令统一见 [包说明](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/README.md)。
2. **人工复核与模式升级。** 事件候选仍为保存的 7 条、reviewed=0；未生成或代填人工标签。X 仍需至少 20 条人工复核且误判率不高于 2%；Mac annotate 还缺当前 generation 校准回执、200 条人工标注、7 天 shadow 与可用性证据。Jev 正式抑制仍要求 200 条人工确认后再决定。覆盖率达标只完成其中一项。
3. **既有 media ledger 损坏。** r4s 权威源第 177 行 JSON 校验失败，当前错误为第 221 列缺少逗号分隔；Mac 保留 176 行 last-good。本机日志显示该故障始于 2026-08-18 18:40:58，当前形式自当日 22:06:35 已出现，早于整改。sent-content 权限错误已消失并同步成功，但 wrapper 的退出码仍为 1，以保留 media 失败信号。[日志摘录](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/evidence/remediation-ledger-log-extract.txt) 保留时间和行号；未修改远端权威账本。

## 文档与交付

有效规则集中在 [去重运行指南](/Users/Apple/Projects/chat-daily-tg/docs/dedup-policy.md)。README、架构和运维手册同步了两类账本方向与 X 镜像入口；首次实现记录追加复查链接。首次测试、失败数据和原始候选保留，带日期报告明确属于历史快照。outputs 中的运行指南与首次记录改为导航文件；本报告通过输出目录软链接指向仓库真源。

[X 修复包](/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup-2026-09-23.zip) SHA-256：`390ea747a98c6c69a51ce4ac9a49037c26ece68aa873b4cf7a0f8c086c6abc2d`。

本次修改保留工作树中原有未提交内容。恢复入口是关闭运行配置中的镜像导入并保持 report；如需恢复派生索引，在停写窗口使用上述一致备份。关闭导入不撤销历史记录，镜像行随已有有效期退出候选。
