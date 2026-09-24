# 去重整改发布范围（2026-09-24）

本版本从 `origin/master` 的 `65bcde09d5152d5a69ec1909b54b804baab8379a` 提取频道去重整改及其运行依赖。配置、密钥、消息、账本和向量数据继续保存在各主机的运行目录。

## 功能

DeliveredIndex 导入 X caption 镜像时保留已有 tg-cli 正文、向量、generation 元数据和独立来源身份。镜像记录按有效期退出候选，其他 producer 不被改写。同步及索引故障继续放行消息。

L2 使用 Jev 主裁判和原 LLM 回退；report 模式记录候选动作并投递。只有无新增事实时才可能抑制，少量增量保留并标注前文，实质增量直接投递。媒体 caption 最多触发标注；终态抑制必须先成功写入 journal。generation、覆盖率、reranker 和正式校准回执继续参与模式门控。

频道 wrapper 持锁后刷新 X caption 镜像，同步失败仍执行频道任务。每分钟 ledger-sync 处理 r4s media 拉取与 Mac sent-content 推送。频道排期增加 00:00、02:00。

## 文件与依赖

| 范围 | 文件或符号 | 纳入原因 |
| --- | --- | --- |
| L2 索引与门控 | `topic_dedup.py`、`evidence_index.py`、`vector_math.py` | 镜像导入依赖 generation 元数据、向量校验、覆盖率和校准回执 |
| 模型运行 | `inference_queue.py`、`qwen_runtime.py`、`model_identity.py`、`jev_*.py` | 保留现有有界请求、Jev 回退及本机 Qwen 按需生命周期 |
| 配置与接线 | `config.py`、`paths.py`；`application.py` 的 `_build_dedup_gates`、`_push_raw_channels` | 发布去重配置并连接频道入口 |
| 投递及 caption | `raw_channels.py`、`private_media.py`、`content_seen.py`、`raw_seen.py`、`tg_sender.py`、`telegram_exporter.py` | caption 处理依赖现有跨频道顺序、媒体身份、失败孔洞、歧义投递和增量同步接口 |
| 账本 | `dedup_journal.py`、`sent_content_mirror.py`、`sent_content_ledger.py` | 终态可追溯、原子镜像及 r4s 只读副本 |
| 请求脱敏 | `sanitize.py` | Jev 输入复用现有凭据脱敏逻辑 |
| 运维入口 | `scripts/backfill_delivered_embeddings.py`、`sync_xmonitor_sent_content.py`、`sync_sent_content_ledger.sh`、频道及 ledger wrapper、`calibrate_topic_dedup.py` | 校验、限时回填、同步与调度 |
| 抓取辅助 | `scripts/tg_public_backfill.py`、`tg_media_dump.py` | 配套增量抓取与媒体 manifest 接口 |
| 排期 | `schedule.yaml`、channels plist | 保留已确认的夜间频道轮询 |

`model_identity.py` 只提取模型版本指纹函数；该函数的算法与现有本机实现一致。知识库索引及其 numpy、tokenizers 依赖未进入本版本。`application.py` 的日报、成长与媒体 digest 实现沿用基线；微信导出、知识库、AI91 monitor 和原工作树中的其他改动未纳入。

## 验证

在独立发布目录、Python 3.13.5 中验证：去重、频道、Jev 定向测试 325 项通过；补齐抓取脚本后完整测试 844 项通过，耗时 35.15 秒。网络和 Telegram 调用由测试替身隔离。Python 编译、shell 语法及 `git diff --check` 通过。

密钥扫描覆盖待提交文件及新增文本；凭据格式命中均为环境变量、正则或测试占位符。提交树未包含 `.env`、运行日志、SQLite、消息账本或人工复核标签。

## r4s 兼容范围

2026-09-24 检查时，r4s 运行目录没有 Git，且 `topic.enabled=false`。本版本只记录 r4s 与 Mac 的接口和兼容要求，r4s 现运行代码的哈希清单另行保存。部署时分别保留 r4s 的 `config.yaml`、`.env`、cron 和权威 media ledger；Mac 的 launchd、Qwen 按需入口和频道配置不能整树覆盖到 r4s。
