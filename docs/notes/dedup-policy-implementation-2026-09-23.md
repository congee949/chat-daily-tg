# 去重策略实现记录（2026-09-23）

本记录保存 2026-09-23 首次实现时的源码验证与安装结果；后续发现与修复见文末复查记录。有效规则见 [去重运行指南](../dedup-policy.md)，审计样本见 [dedup-policy-audit-2026-09-23.md](../dedup-policy-audit-2026-09-23.md)。

## 变更范围

- DeliveredIndex.ingest_sent_ledger() 增加 BWG sent-content.v1 caption 快照导入，快照校验、原子替换、24 小时有效期和 producer/目标群组过滤集中在 sent_content_mirror.py。
- L2 的 none、minor、substantial 分别产生 skip、annotate、deliver 基础动作；report/annotate 和覆盖率等门槛继续限制最终动作。媒体最多标注，skip 的 journal 写入失败则交付。
- 公开与私有媒体的 caption 进入 L2；公开频道从媒体下载 manifest 恢复缺失 caption，成功发送后登记实际承载正文的消息 ID。
- 频道调度增加 00:00、02:00；Mac launchd 已安装并核对。模板、README、架构和 runbook 同步更新。
- 镜像同步 CLI 已接入 ledger-sync wrapper，镜像失败记录独立退出码；限时向量回填支持 --sent-ledger。
- BWG 对应源码补丁实现纯翻译回复、显式官方账号组引用折叠，以及官方文本自回复合并和完整成员重试。

## 现场证据

[dedup-event-review-2026-09-23.json](dedup-event-review-2026-09-23.json) 保存从 BWG 只读读取的 would_suppress 候选。文件中共有 7 条记录，reviewed 均为 0。它们没有被当成人工标签，也没有触发 enforce。

BWG caption 账本在本次只读检查中有 1,326 条有效 JSONL 记录。通过 scripts/sync_xmonitor_sent_content.py --source-file 写入临时快照后，再以 --check 读取，报告为 1,326 条、schema chatdaily.sent-content-mirror.v1、状态 fresh。快照在隔离工作目录验证，未作为运行配置写入。
Mac 当前只读状态显示 delivered_index 已安装镜像字段，597 条记录来自镜像；最近 48 小时共有 111 条 x_monitor 记录，其中 0 条带当前 generation 的向量。backfill_delivered_embeddings.py 的 dry-run 报告 eligible=1361、valid=730、missing=631、coverage=0.5364，返回维护状态码 3；未执行 --apply，因此数据库没有因本次验证写入。L2 仍按 report/fail-open 工作，镜像记录在完成回填前不会成为向量候选。

## 验证

- Mac 去重、镜像、调度、私有媒体、公开频道和 journal 定向回归：211 passed。
- Mac 完整回归：1399 passed。
- 镜像、回填和 L2 定向回归：122 passed。
- X monitor 完整回归：503 passed（BWG 源码隔离副本）；对应补丁已保存，未部署远端。
- Python 编译检查覆盖 src/chat_daily_tg 和新增同步脚本。
- git diff --check 通过。
- X 侧 [补丁](x-monitor-dedup-implementation.patch)在对应 BWG 基线快照实际应用，产物与被测试源码逐字节一致。

## 启用状态

Mac channels 排期已通过 schedule.py apply 安装，00:00、02:00 与原有各时段均已核对。其余 3 个日历 label 字节未变，未重载。Mac 源码已经落盘，现有 wrapper 下一轮会加载；未为本次验证主动发送 Telegram 消息。

BWG 补丁按 /root/x_monitor 的审计源码编制，未写入远端运行目录。本机 /Users/Apple/Projects/x_monitor 是另一版本，保留原有文件。部署时应核对基线哈希、备份受影响文件、应用补丁，再显式配置相关开关。

事件账本仍缺人工标签；Mac L2 保持 Jev 主裁判、原 LLM fallback、report 与 enforce_enabled=false。镜像正文入库后需要完成向量回填、校准和观察门槛，才可进入 annotate。正式抑制分别遵守 X 的 20 条复核门槛与 Jev 的 200 条人工确认门槛。

审计规则 C 的员工限制策略与规则 A 是备选关系：选择显式账号组的 A，保留员工账号现有个人内容订阅。方案 4 的跨 producer 标题指纹属于附加优化，当前 caption 镜像覆盖 X；MacRumors 仍需独立确认可用账本与标题来源后接入。信息图采用保留并最多标注的策略，图片内容不能由 caption 子集判定为终态重复。

## 2026-09-23 复查发现

[复查与整改计划](dedup-review-and-remediation-2026-09-23.md) 发现镜像覆盖既有向量、同步脚本缺少执行权限、每分钟重复拉取 X 账本，以及 X 官号顺序和 idle 等待范围的问题。首次实现的 1399 项 Mac 测试未覆盖这些条件；本页前面的数量和启用状态保留为当时快照。

向量保留、同步调用位置、X 账号顺序工具和事件类型白名单已修复。00:00、02:00 继续保留。代码、生产恢复、测试和未达成的人工门槛统一记录在 [整改实施报告](dedup-remediation-implementation-2026-09-23.md)；有效配置与命令以 [运行指南](../dedup-policy.md) 为准。
