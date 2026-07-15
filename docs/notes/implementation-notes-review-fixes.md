# Implementation Notes — 对抗式审查修复 (2026-06-29)

承接仓库根的 `adversarial-review-2026-06-29.md`。范围：P0 + P1 全部（用户确认）。数据层：直接迁 SQLite（用户确认）。

## Design Decisions

### 数据层 SQLite 迁移（发现 1/2/3/40/43）
- **单文件共享 DB**：`DATA_DIR/chat-daily.db`，三张表 `permanent` / `hot_leads` / `repeat_topics`（遵循报告 FP「单文件」建议）。
- **连接策略**：新增 `sqlite_util.py`，统一 `connect(db_path)`（WAL + synchronous=NORMAL + busy_timeout=5000 + Row factory）与 `init_schema(conn)`。每次操作开短连接（低频管道，无需长连接池）。
- **保留 dataclass 与方法名**：`PermanentEntry/HotLead/RepeatTopic/TopicMention` 不变；`PermanentDB.read_all/upsert_many/upsert/append/find/mark_status`、`RepeatTopicDB.read_all/upsert_many`、`recent_repeat_summary`、`append_day_leads/load_all_leads/mark_lead_status/regenerate_latest` 方法名与语义保持，只换存储后端 → 调用方几乎不动。
- **去重键**：`permanent.fingerprint` 加 `UNIQUE` 约束（发现 3 的根治位点）；upsert 在单事务内按 fingerprint 查存在性，沿用 `_merge_one` 语义（mention_count+1、last_mentioned_at、truthy 字段覆盖）。`hot_leads` 主键 `id`，`ON CONFLICT(id) DO UPDATE`（修发现 40 的同 id 重复）。`repeat_topics` 主键 `id`，沿用 seen_date 去重。
- **坏行容错（发现 2/43）**：SQLite 行存储，无逐行 `json.loads`，坏行崩溃问题按构造消失；`seen_dates` JSON 列解析加防护。
- **URL 规范化（发现 3）**：`compute_fingerprint` 在 url 存在时先 `_canonical_url()`：剥离 utm_*/from/share*/spm/fbclid 等跟踪参数 + fragment，lower host，去尾斜杠，保留承载身份的 query（如 id），其余 query 排序后并入。title-based 路径不变。

### LLM 解析分层降级（发现 33/34/37/39）
- 见 summarizer 包，待实现时补记。

### 编排幂等（发现 40/42/43）
- 持久化前置 `.persisted` 标记门，catch-up 重跑跳过持久化只重生成视图+推送（修发现 40 的非确定 id 重复）。
- 派生视图重生成 + death_signals 包 try/except，失败不阻断推送（发现 43/35）。
- 多 chunk 推送原子性（发现 42）：待定方案（落 `.pushed` 标记 / 收紧长度）。

### LLM 解析分层降级（发现 33/34/37/39）— 已实现
- #37 fence 解析：非贪婪正则 → 行级深度状态机，正文内 ```python 代码块不再截断；仍保留未闭合(截断)与首次出现优先语义、untagged json verification 兜底。
- #34：verifier 输出(含其 repair)解析失败 → 回退发布 initial 草稿(verification={"error":"verifier_parse_failed"})，不再整天零产出。
- #33：repair 二次解析失败 → `_best_effort_summary` 从原始/repair 文本尽力提取 concise(opportunities 提不出则空)，只有连 concise 都没有才 raise。
- #39（保守版）：`_persist_initial_draft` 在 verifier 前把 initial concise/detailed 落 archive(initial-concise.md/initial-detailed.md)，verifier 误删真信息可恢复。

### 编排幂等/隔离（发现 40/42/43）— 已实现
- #40：`.persisted` 标记门 + 持久化抽到 `_persist_opportunities`；catch-up 重跑(推送失败后)跳过重持久化，避免 hot_leads 非确定 id 重复入库。
- #43：持久化 + 派生视图重生成整段包 try/except(non-fatal)，失败只 log 不阻断已生成报告的推送；SQLite 行存储本身已消除 load_all_leads 坏行崩溃源。
- #42：`tg_sender.send(state_path=...)` 多 chunk 推送按 payload-hash 守护断点续传；同日 catch-up 内容不变则从断点续发(不重发前半)，内容变了则整发(罕见、可接受)。

### 可靠性/告警/外部依赖（发现 10/13/14/16/19/20/30）— 已实现
- #10：llm_client 把 (ValueError,KeyError,IndexError) 纳入重试 → 200 但畸形/缺 choices 的软失败可重试。
- #30：logging_setup 暴露 `redact()`；notify_failure 对 title/message 先脱敏再 osascript/TG。
- #19：notify_failure 增 best-effort TG(经 1082 代理 + alert 话题路由)，由 `CHAT_DAILY_TG_ALERTS` 门控(测试/即兴运行不触发；guarded wrapper 置 1)。
- #14：guard_common.sh `guard_setup_env` 导出 HTTP(S)_PROXY=1082 + NO_PROXY=localhost/127.0.0.1/::1(放行本地 qwenproxy:3000)，daily 与 channels 两个 wrapper 都调用。
- #16：新增 run_channels_guarded.sh + guard_common.sh(共享 notify/env)，channels plist 改走 `cdrun-bash run_channels_guarded.sh`，消除 exit-127 静默。
- #20：dump_channel 加 kabi python `os.access` 预检；push_raw_channel_cards 统计私有频道全失败 → notify_failure。
- #13：mixed album 部分失败时 `_send_media` 回传失败计数；因增量 HWM 是 max(见下 Tradeoffs)，仍记 seen 但 notify 一次，把静默丢失变成可见(一次性)告警。

### 部署/安装脚本（发现 17/18/21/22）— 已实现
- #17：deploy.sh 删除错误 label 的自制 plist 逻辑，改调 scripts/install-launchd.sh。
- #18：BRANCH 默认当前分支(DEPLOY_BRANCH 可覆盖)；reset --hard 前 `require_clean_tree` 拦截未提交改动。
- #21：依赖安装 `pip ... || true` → `uv sync`(失败即 exit，set -e)。
- #22：install-launchd.sh 抽 `install_label()`，安装 agent + channels 两个 label。

### 迁移与验证
- scripts/migrate_jsonl_to_sqlite.py：备份(.bak-<ts>) + 坏行容错导入 + INSERT OR IGNORE 折叠 fingerprint/id 重复 + --force/--dry-run + 已有数据拒绝(除非 --force)。
- dry-run 实测：permanent 188 / repeat_topics 526 / hot_leads 234，0 坏行。**迁移尚未在 live 数据上执行**(见 Open Questions)。
- 全量测试 200 passed（原 193 + 新增 7）。

## Deviations
- `scripts/archive/migrate_permanent_dedup.py` 在 `archive/`，是已归档的一次性脚本，不维护其兼容性（继续读 jsonl 即可，迁移后不再使用）。
- **#13 偏离报告字面方案**：报告建议「部分失败不写 seen 让下次重试」。但增量 high-water mark 是 `max(seen ids)`，后续整发成功的帖会把 HWM 推过失败帖 → 「不写 seen」对批次中间帖实际不触发重试。按第一性原理改为：照常记 seen(与 HWM 一致) + 一次性 notify，把静默丢失变可见，避免每 2h 重发已成功媒体的 spam。
- **#19 实现方式**：未把网络逻辑硬塞进 notify_failure 的默认行为(会污染单测/即兴运行)，改为 env 门控 + guarded wrapper 兜底，等价达成「channels 失败必达 TG」。
- **#39 保守实现**：只做「保留 initial 草稿可恢复」，未做报告建议的「verifier 仅标注 + 代码侧最小改写」完整重构——后者改变每日推送正文(产品决策)，留作可选(见 Open Questions)。

## Tradeoffs
- **单文件 vs 每 store 一文件**：选单文件（报告建议 + 未来跨表事务空间 + 单一 WAL）。代价：调用点统一改用 `DB_PATH`，存储测试需重写（已预期，存储后端变更的正常成本）。
- **保留每日 md（hot_leads YYYY/MM/DD.md）**：数据进 DB 后该 md 冗余，但删除超出报告范围，按「surgical」保留，由 `append_day_leads` 的 `md_root` 显式参数驱动（默认 None 跳过）。
- **#40 残留边界**：仅当「持久化中途异常 且 推送也失败」的双重失败下，catch-up 重跑可能重复 append hot_leads(首次部分写入的情况)。常见场景(全持久化成功后推送失败)已被 .persisted 完全覆盖；该残留边界罕见，未做每步 marker/跨表事务。
- **#42 残留**：LLM 文本在 catch-up 重跑时非确定 → hash 不匹配则整发(可能重发)，属可接受(内容已变=纠正版)。
- **告警冗余**：in-Python 优雅失败时 notify_failure(TG) 与 wrapper(非零退出 TG) 可能各发一次 = 2 条 TG。视为告警系统的安全冗余，未消除。

## 自审复核轮（6-agent adversarial verify 后修）
对完成的 diff 跑了一轮对抗式自审（6 路 + 综合），结果：0 新 P0 / 0 not-fixed / 0 regression(严格计数)，但综合发现 2 个我在 #37 重写里引入的真 P1 + 迁移脚本一个静默丢数据 bug。已全部复现并修复：
- **P1-1（已修）**：`_extract_fences` 深度计数器遇 body 内**非平衡**内层 fence(奇数 ```) 时 depth 永不归零，吞掉其后所有顶层块 → opportunities/verification 静默丢失。修法：加 `_KNOWN_TOPLEVEL` 已知顶层 opener 作硬边界，下一结构块 opener 即终止当前块(不消费)，当前块闭合缺失/失衡也不殃及后块。已加回归测试。
- **P1-2（已修）**：新 `_extract_fences` 用 first-wins，破坏「opportunities 与 verification 都输出为无标签 ```json」时 verification 兜底(同 key (json,'') → 丢 verification)。改回 last-wins(对齐旧闭合 fence 语义)。已加回归测试。
- **迁移静默丢数据（已修）**：INSERT 行原从 raw rec 取值，旧记录缺 NOT-NULL-默认字段 → None → OR IGNORE 静默丢行。改为从 `asdict(已验证 dataclass)` 取值；报告 skipped 计数；dry-run 不再遗留空 db。scratch 实测通过。
- **顺手补的低优先项**：persist/regen 失败 except 加 notify_failure；#13 媒体丢失告警按频道聚合一次(对齐注释「once」)；deploy.sh 加 detached-HEAD 守卫；补 notifier 门控/脱敏 + kabi 预检测试。
- 全量测试 206 passed（基线 193 + 新 13）。

## 迁移执行（已完成 2026-06-29）
用户确认后在 live ~/chat-daily 执行。全部 jsonl 先打 .bak-20260629-151321 备份。
- **迁移中发现并修复一个真数据丢失 bug**：hot_leads id 是位置式 `{date}-hot-NNN`，旧 blind-append 把同日多次 catch-up 重跑写进同一天文件 → 同 id 映射到**不同**内容（实测 234 行中 47 个 id 有碰撞、95 个碰撞行**全部内容不同**、0 个字节相同）。首版 INSERT OR IGNORE-by-id 静默丢了 95 条 distinct 历史 lead。改为：按完整内容签名去重(只折叠字节相同)，位置式 id 碰撞则 re-id(`{id}-rN`)，保全所有 distinct lead。
- 最终入库：permanent 187(188 输入，1 个 canonical-fp utm 重复合并、mention_count 求和)、hot_leads 234(95 re-id、0 丢)、repeat_topics 526。
- 读回验证：三库 read_all 计数正确、234 个 hot_lead id 全唯一无 PK 碰撞、permanent.md/latest.md 派生视图正常重生成、context_builder 正常、mention_count 合并生效(max=3)。
- 注：这些 4 月历史 hot_leads 都在 14 天保留窗外，不影响当前 latest.md 输出，但迁移按数据保真原则全保留。

## 仍需用户/部署侧注意
- **部署 plist**：launchd 现仍跑旧 plist(channels 直调 python)。要让 #16/#14 等 plist 改动生效，需 `bash scripts/install-launchd.sh`(装 agent+channels 两个 label)。未执行——属部署动作，按需。
- **#39 完整重构**(verifier 仅标注 + 代码侧最小改写)：用户选择保持现状(保守版已实现)。

## Tradeoffs
- **单文件 vs 每 store 一文件**：选单文件（报告建议 + 未来跨表事务空间 + 单一 WAL）。代价：调用点统一改用 `DB_PATH`，存储测试需重写（已预期，存储后端变更的正常成本）。
- **保留每日 md（hot_leads YYYY/MM/DD.md）**：数据进 DB 后该 md 冗余，但删除超出报告范围，按「surgical」保留，由 `append_day_leads` 的 `md_root` 显式参数驱动（默认 None 跳过）。

## Open Questions
- （暂无，scope/数据层方案已由用户拍板）
