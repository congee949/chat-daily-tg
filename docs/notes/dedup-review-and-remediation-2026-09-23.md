# 去重实现复查与整改计划（2026-09-23）

对象：`/Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs` 交付的去重实现（Mac 源码改动 + BWG `x_monitor` 补丁包）。
前置文档：[审计](../dedup-policy-audit-2026-09-23.md)、[实现记录](dedup-policy-implementation-2026-09-23.md)、[运行指南](../dedup-policy.md)。

整改实施结果见 [整改实施报告](dedup-remediation-implementation-2026-09-23.md)。以下复查结论保留检查时的状态。

本文分两部分：第一部分是复查结论（发现了什么、证据是什么），第二部分是按优先级排列的整改步骤（每步的目标、具体改法、验证、回退）。所有事实来自 2026-09-23 21:30–21:45 的只读检查；生产状态以当时快照为准。

## 一、复查结论

总体判定：**代码质量和测试证据可信（Mac 1399 passed 本机复跑一致；BWG `twitter_monitor.py` 哈希与 `checksums.json` 基线一致），但存在两个已经作用于生产的回归，以及一个使核心方案在目标场景失效的设计缺口。不应按"已完成"验收。**

### 1.1 阻断项

**A. Mac `delivered_index.db` 中原有 x_monitor 向量被清空。**

`DeliveredIndex.ingest_sent_ledger` 对已存在行执行 `ON CONFLICT(msg_id) DO UPDATE SET text=excluded.text, embedding=NULL, …`。tg-cli 进货的文本卡（`📢 @dotey[​](url)`）与 BWG caption（`📢 @dotey​`）字面不同，`old["text"] == text` 不成立，于是已有向量的行被覆盖并置空。

证据：

```
# 8-27 备份
sqlite3 delivered_index.db.pre-qwen-strict-20260827-0854 \
  "select count(*), sum(embedding is not null) from delivered where producer='x_monitor';"
→ 27|26
# 当前
sqlite3 delivered_index.db \
  "select count(*), sum(embedding is not null) from delivered where producer='x_monitor';"
→ 597|0
```

`config.py` 中 `xmonitor_ledger_enabled` 默认 `True`，因此这次写入在 `~/chat-daily/config.yaml` 未改动的情况下由生产 channels 任务自动执行。coverage 从 0.990 降到 0.536，距 annotate 门槛（0.995）更远。

**B. `run_ledger_sync_guarded.sh` 每分钟报错。**

wrapper 新增调用 `scripts/sync_sent_content_ledger.sh`，该文件 mode 644。`guard-ledger-sync-2026-09-23.log` 每 60 秒一条 `Permission denied`，`end ledger-sync exit=1`。同一 wrapper 每分钟 ssh 到 bwg 拉取 1.6 MB 账本并重写 `xmonitor_sent_snapshot.json`（mtime 每分钟更新，无"内容未变则跳过"逻辑）。

**C. launchd `channels` 已 reload 并新增 00:00、02:00。**

审计文档将夜间排期列为"由用户决定"，AGENTS.md 规定未经明确要求不修改 launchd。交付报告对此描述准确，但归类为"已完成"而非"待确认"。若未单独授权，需回退。

### 1.2 设计缺口

**D. 官方圈引用折叠受账号处理顺序限制。**

`quote_fold.plan` 只在被引原推已 `confirmed` 时才折叠。`x_monitor` 按 `twitter_accounts.json` 顺序处理，`ClaudeDevs` 排在 `claudeai` 之前。2026-09-22 17:00 UTC 那轮的实际顺序是 ClaudeDevs Quote（17:00:03）先于 claudeai 原推（17:00:08）。补丁部署后，同样的发布日 ClaudeDevs 仍发独立卡。`trq212`、`bcherny` 排在后面可以折叠，但 ClaudeDevs↔claudeai 这一对不行。`test_quote_fold.py` 没有覆盖"原推在同轮稍后送达"的用例。

**E. 线程合并的 90 秒 idle 延迟所有官方文本推文。**

`thread_merge.merge_ready` 对任何带 `push_policy` 的账号、无媒体、创建时间 <90 秒的推文都判为 active 并转入 retry，包括单条 `quota_reset`。2026-07-18 设计明确 `quota_reset`、`quota_compensation` 首条立即发，只有 `dev_release`、`model_api`、`model_launch` 等待 idle window。此外首条带图即不合并，而官方发布首条几乎都带图，实际覆盖面只剩纯文本的额度/政策类 thread。

### 1.3 次要

- `outputs/dedup-guide.md`、`outputs/implementation-record.md` 与仓库 `docs/dedup-policy.md`、`docs/notes/dedup-policy-implementation-2026-09-23.md` 是同一内容的两份拷贝，后续会漂移。
- `sync_xmonitor_sent_content.py` 走 `ssh cat`，未复用仓库其他同步脚本的 `SSH_OPTS` / `StrictHostKeyChecking=accept-new` 约定。
- `evidence/x-tests.log` 结尾只有告警行，未见 `OK` 汇总；X 侧 503 项通过未在本机复跑。

### 1.4 做得对的部分

- BWG 未部署；`checksums.json` 与远端一致；`verify_baseline.py` 可用；补丁只加新文件和主模块接线，回滚清晰。
- fail-open 边界守住：journal 写失败→deliver；ambiguous 不作锚点；目标 chat/thread 隔离；无自有媒体才终态。
- 报告对未完成事项（未部署、未回填、report/observe 未升级、reviewed=0）写得诚实。

## 二、整改步骤

优先级一览：

| 步骤 | 性质 | 预计时长 | 不做的后果 |
|---|---|---|---|
| 0 冻结镜像 | 止血 | 2 分钟 | 每轮 channels 继续覆盖索引 |
| 1 chmod | 止血 | 1 分钟 | ledger-sync 每分钟报错 |
| 2 夜间排期去留 | 决策 | 视用户 | 已生效，凌晨会推 |
| 3 修覆盖逻辑 + 回填向量 | 修复回归 | 1–2 小时 | L2 候选面比改动前更差 |
| 4 X 侧顺序 + idle 豁免 | 部署前置 | 1 小时 | 折叠在目标场景不触发；额度告警晚 30 分钟 |
| 5 事件账本人工复核 | 推进 | 30 分钟/次 | 事件账本永远 observe |
| 6 Mac L2 升 annotate | 推进 | 数周积累 | L2 只记不标 |
| 7 文档整理 | 整理 | 20 分钟 | 记录与现实不一致 |

0、1 立即执行；2 当天决定；3 完成后再谈 4 的部署；5、6 是持续动作。

### 第 0 步：冻结镜像导入

目标：在修复前阻止每轮 channels 继续覆盖索引。

操作：`~/chat-daily/config.yaml` 的 `sources.telegram.dedup.topic` 下显式加：

```yaml
xmonitor_ledger_enabled: false
```

`application.py` 据此把 `sent_ledger_path` 置 `None`，`ingest_sent_ledger` 不再执行。不需要重装 launchd，下一轮 channels 生效。

判断依据：report 模式下 L2 不抑制，关掉镜像不损失投递，只是回到 9-22 之前的候选面。

### 第 1 步：ledger-sync 止血

```bash
chmod +x /Users/Apple/Projects/chat-daily-tg/scripts/sync_sent_content_ledger.sh
# 等下一分钟
tail -n 8 ~/chat-daily/logs/guard-ledger-sync-2026-09-23.log
```

期望：三段（media / content / xmonitor）都不再出现 `Permission denied`。

附带发现：日志中另一条 `pulled ledger failed JSONL validation; keeping last-good`（media ledger 来自 r4s）在本次改动之前就存在，它让 `exit=1` 一直为红，会掩盖新问题。建议单独排查 r4s `/root/chat-daily/state/media_sent_ledger.jsonl` 为何校验失败；否则 ledger-sync 的退出码没有信号价值。

### 第 2 步：决定夜间排期去留

这是产品决策。两个事实：

- 00:00/02:00 只解决"在花 00:30 发帖等到 06:09"这类延迟，不解决重复；重复依赖第 4 步之后的 L2。
- 代价是凌晨两次推送把所有频道内容（不只重大发布）提前推到手机。

若不要：

1. `schedule.yaml` 删掉 `"00:00"`、`"02:00"`；
2. `launchd/com.chat-daily-tg.channels.plist` 删对应两个 `<dict>`；
3. `uv run python scripts/schedule.py apply`。`apply` 自带 in-flight 检测，channels 运行中会跳过并提示，不要用 `--force`。

若保留：至少观察一周夜间实际推送条数，再决定是否改为"仅当 x_monitor 当晚有 `model_launch` 事件时触发补拉"。后者需要跨机器信号，复杂度高，不建议现在做。

### 第 3 步：修 `ingest_sent_ledger` 覆盖逻辑，再补回向量

**3a. 不覆盖已有向量的行。** `topic_dedup.py` 的 `ingest_sent_ledger`，`old is not None` 分支改为：

- `old["mirror_source"] is None` 且 `old["embedding"] is not None`（tg-cli 已进货且已有向量）：只 `UPDATE mirror_source, mirror_valid_until`，不改 `text` / `norm_text` / `embedding`。tg-cli 正文与 caption 的差异只是零宽锚，`normalize_for_embedding` 后基本一致，无需重新嵌入。
- `old["embedding"] is None`（tg-cli 进货但无向量，或 caption 为空的图片卡）：允许用镜像正文替换。
- 更稳的判据：比较 `normalize_for_embedding(old_text) == normalize_for_embedding(text)`，相同则不动向量。

补测试：`tests/test_topic_dedup.py` 增加"已有向量的 x_monitor 行被镜像再次导入后向量保留"用例。

**3b. 默认关闭。** `config.py`：`xmonitor_ledger_enabled: bool = False`。理由：会写生产 sqlite 的功能不应随代码落盘自动开启。同步更新 `docs/dedup-policy.md` 配置示例和 `tests/test_config.py`。

**3c. 补回向量。** 修完 3a 后用 sidecar 回填，不要靠 channels 轮次的 `online_backfill_cap=32` 慢慢追（597 行需要约 19 轮）：

```bash
cp ~/chat-daily/state/delivered_index.db ~/chat-daily/state/delivered_index.db.bak-20260923-pre-backfill
cd /Users/Apple/Projects/chat-daily-tg
# dry-run
env -u ALL_PROXY -u all_proxy uv run python scripts/backfill_delivered_embeddings.py \
  --db ~/chat-daily/state/delivered_index.db --config ~/chat-daily/config.yaml \
  --sent-ledger ~/chat-daily/state/xmonitor_sent_snapshot.json
# apply，约 3 轮
env -u ALL_PROXY -u all_proxy uv run python scripts/backfill_delivered_embeddings.py \
  --db ~/chat-daily/state/delivered_index.db --config ~/chat-daily/config.yaml \
  --sent-ledger ~/chat-daily/state/xmonitor_sent_snapshot.json --apply --max-rows 256 --max-seconds 600
```

回填避开 channels 整点触发（sqlite 有 busy_timeout，但 Qwen runtime 会被两个进程抢）。

验证：

```bash
sqlite3 ~/chat-daily/state/delivered_index.db \
  "select producer, count(*), sum(embedding is not null) from delivered where ts>='2026-09-16' group by producer;"
```

目标：x_monitor 行第三列接近第二列；整体 coverage ≥ 0.995。

**3d. 每分钟拉取。** 二选一：

- `sync_xmonitor_sent_content.py` 加"远端 mtime/size 未变则跳过"（`ssh bwg stat -c '%Y %s' …`）；
- 把 xmonitor 同步从每分钟的 ledger-sync 拆出，挂到 `run_channels_guarded.sh` 开头（每天 9–11 次，正好在 L2 使用前刷新）。后者更符合"用之前才拉"的语义。

### 第 4 步：X 侧补丁部署前必须补的两处

**4a. 账号处理顺序。** `twitter_accounts.json` 是有序数组，`main` 按顺序遍历。把产品官号提到开发者号之前：`claudeai` → `ClaudeDevs`；`OpenAI` → `OpenAIDevs` → `thsottiaux`。同轮内原推先 confirmed，Quote 进入 `quote_fold.plan` 时能查到锚点。只改配置。

补测试：`test_quote_fold.py` 增加用例——ledger 无 source 时 `plan` 返回 deliver，插入 confirmed 后再调用返回 reply/skip。把顺序依赖写成测试，以后调整账号顺序时会被提醒。

**4b. idle 延迟只对指定事件类型生效。** `_push_event_type` 在 `classify_official_push` 之后已挂在 tweet 上（`twitter_monitor.py` 约 5302 行），`merge_ready` 调用点在其后，可以直接读取。`active` 判断增加条件：

```python
tweet.get("_push_event_type") in {"dev_release", "model_api", "model_launch", "major_product_launch"}
```

其余（`quota_reset`、`quota_compensation`、`credit_grant`、`quota_policy`、`plan_entitlement`、`model_access`）不延迟，与 2026-07-18 设计 §5.1 一致。

已知限制（记录不修）：首条带图即不合并。写进交付包 README。

**4c. 部署步骤补充。**

- 窗口选 cron 的 `:05–:25`（monitor 每半点跑，超时 25 分钟）；先 `pgrep -f twitter_monitor.py` 确认无在跑。
- `config.fragment.json` 三个开关分阶段开：先 `translation_reply_enabled`（影响面最小），观察两天 `.semantic_decisions.jsonl` 中 `translation_source_delivered` 条数和 Telegram 效果；再 `official_quote_groups`；`official_thread_merge_enabled` 最后。
- 部署后手动 `run.sh --dry-run`，确认日志中三个开关的加载行和 quote-fold 输出，再交回 cron。

### 第 5 步：事件账本人工复核

不改代码。`event_ledger_review.py list` 列出 `would_suppress` 候选（当前 7 条，含 claudeai 19:00 那条）。逐条对照 X 原文判断"是否确实无新增结构化事实"，`label ID --valid-suppression` 或 `--false-positive`。达到 20 条且误判 ≤2% 后，`config.json` 的 `event_dedup_mode` 改 `enforce`，代码内 Go/No-Go 自动核对。

7 条不够，需在后续发布日积累。GPT-6 Sol/Luna 发布当晚 OpenAI 系账号的观察记录应也在其中，可一并标注。

### 第 6 步：Mac L2 升到 annotate 的条件

需同时满足：

1. coverage ≥ 0.995（第 3 步回填后可达）；
2. `calibrated_generation_id` 填当前 generation（`embedding-79d71052b05a76ed1879`）；
3. `topic-dedup-calibration-receipt.v1.json` 存在且通过 `validate_calibration_receipt`：200 条人工标注、7 天 shadow、可用性 ≥0.995。7-16 的 markdown 报告不被接受。

主要卡点是 200 条标注。第 3 步完成后 report 模式的 journal 会开始出现有意义的 `judge-none` / `minor` 记录，它们就是标注的原材料。

### 第 7 步：文档整理

- 仓库版本作为唯一事实源。在 `docs/notes/dedup-policy-implementation-2026-09-23.md` 追加一节"2026-09-23 复查发现"，链接到本文，避免后来者以为镜像功能是干净上线的。
- `outputs/` 只保留 `x-monitor-dedup/`、`evidence/`、`event-review-candidates.json`；`dedup-guide.md`、`implementation-record.md` 删除或改为指向仓库文档的链接。

## 三、复查用命令（只读）

```bash
# 向量覆盖
sqlite3 ~/chat-daily/state/delivered_index.db \
  "select mirror_source is not null, count(*), sum(embedding is not null) from delivered group by 1;"
# ledger-sync 状态
tail -n 8 ~/chat-daily/logs/guard-ledger-sync-$(date +%F).log
# 排期
uv run python scripts/schedule.py list
# BWG 基线
ssh bwg 'sha256sum /root/x_monitor/twitter_monitor.py'
python3 /Users/Apple/Documents/Codex/2026-09-23/wan-z/outputs/x-monitor-dedup/verify_baseline.py <checkout>
# 账号顺序
ssh bwg 'python3 -c "import json;print([a[\"username\"] for a in json.load(open(\"/root/x_monitor/twitter_accounts.json\"))])"'
```
