# chat-daily-tg 深度对抗式代码审查报告

> 生成于 2026-06-29 ｜ 方法：7 路并行 finder（按攻击面分维度）→ 逐条对抗式验证（每条发现独立 skeptic 复核真实代码 + 可达性）→ 去重分级综合。
> 规模：58 agents，原始发现 50 条，验证存活 45 条（驳回 5 条），P0×4 / P1×19 / P2×22。

这条管道当前最致命的失效模式是 **"沉默"**：它在两个维度上系统性地丢东西又不报警。其一是**数据层的非原子写入**——所有 JSONL store 都用 `open('w')` truncate-then-write 全量重写，崩溃即可能丢失整个机会库（发现 1），且坏行无容错会让库永久不可读（发现 2）。其二是**编排层的"提交点与确认点分离"**——持久化发生在 TG 推送之前、而幂等标记 `COMPLETE_MARKER` 写在推送之后，任何中间失败都让 catch-up 重跑，造成重复入库（发现 40/42）和派生视图重生成炸管道（发现 43）。最隐蔽的是**新加的 channels 转发器把已经修过的 exit-127 静默故障原样重挖了一遍**（发现 16/19/20）——守护与告警能力散落在日报专属 wrapper 里，第二条 launchd 入口完全绕过了它。LLM 解析层则普遍采用"解析失败即 raise 炸全天"的全有或全无策略（发现 33/34/35），手握完好初稿却因核验格式问题整份丢弃。

---

## 第一性原理结论

### 1. JSONL 持久化是否选错 — 选错了，应迁 SQLite

判断不是"JSONL 天生坏"，而是**这个用例的访问模式与 JSONL 的代价正面相撞**。本质需求是：每天 1-3 次低频写、机会条目级数据量（几百到几千行）、核心操作是按 fingerprint 的 upsert 和按 id 的 mark_status、单机单写者但崩溃/中断是常态（venv 消失、休眠唤醒、launchd ExitTimeOut）、失败语义要求"宁可重跑也绝不丢历史机会库"。

JSONL 的根本错配在于它**没有"原地改一条/插一条"的能力**：任何 upsert 或 mark_status 都退化成"读全表→内存改→truncate 重写全表"，把 O(1) 的语义操作变成"拿整库性命做赌注"的 O(全表) 重写——而当前实现连最低成本的 atomic rename 都没搭。

**方向性建议**：迁 SQLite（标准库自带、零依赖、单文件）。upsert 用 `INSERT ... ON CONFLICT(fingerprint) DO UPDATE`、mark_status 用 `UPDATE ... WHERE id=?`、死亡信号批量用一个 transaction，WAL 模式天然抗中断、read 永远看到一致快照；fingerprint 去重落成 UNIQUE 约束比应用层 dict 去重更可靠。`permanent.md`/`hot_leads_latest.md` 等人读视图继续从 SQLite 派生。**迁移前的过渡补丁**：三处 `_rewrite` 统一加 write-temp+fsync+os.replace 原子写、三处 read 加坏行容错、`_run` 入口加 flock。迁移脚本本身是一次高危全库重写，须先带时间戳备份原 jsonl。

### 2. 向量搜索可扩展性 — 当前够用，约 2000 chunk 起进入浪费区，修复成本极低

`evidence_index.search()` 是暴力全表扫描 O(n×dim)：每查询 `SELECT ... FROM chunks` 无 WHERE/LIMIT 取回全部 chunk，每行 `json.loads` 反序列化 768 维向量，再用纯 Python 算余弦；`build_evidence_context_for_summary` 对最多 12 条 claim 各调一次，等于把全表 embedding **反序列化 12 遍**。实测 dim=768：n=500 单查询 ~100ms（×12≈1.2s），n=3000 单查询 ~601ms（×12≈7.2s），基本线性于 n。

**判断**：几百 chunk（典型平静日）无感；~2000 chunk 起每次运行多花几秒；3000+ 线性放大。它**不会让管道中断**（串行、300s LLM 容忍），是明确的、随数据增长持续恶化的性能债。真正的隐性税是同一运行内对全表向量的 12 次重复 JSON 反序列化（占实测耗时大头）。

**方向性建议**：在 `build_evidence_context_for_summary` 开头一次性把全表 embedding load 成 numpy 矩阵 M(n×768) 归一化、12 个查询堆成 Q(k×768)，用 `sims = Q @ M.T` 一次矩阵乘 + argpartition 取 top_k，3000 chunk×12 查询可从 ~7s 降到几十 ms，并消除重复反序列化。进一步可把 embedding 存 BLOB(float32) 替代 JSON 文本。需把 numpy 加进 pyproject 依赖。

### 3. LLM 解析容错策略 — 不充分，"全有或全无"在本管道里是错的

本质：这是一个每天跑一次、把当天**所有群**塞进**同一次** LLM 调用的单体管道。因此"解析失败 → raise → 炸管道"的代价不是某个群丢摘要，而是**当天全部群的日报一起丢失**；且 completion marker 在推送后才写，失败后同日 catch-up 会把整条昂贵的 LLM 管道从头重跑。容错链的真实漏洞：repair 后二次解析裸调无 try/except（发现 33）；验证阶段失败会丢弃已成功的初稿（发现 34）；衍生副产品（death_signals/opportunities）解析或字段为 null 就能在推送前炸掉整天（发现 35/36）。

**方向性建议**：用**分层优雅降级**替代"解析-或-炸"——repair 失败回退到已能提取的部分（哪怕只有 concise），只有连 concise 都提不出才算真失败；verifier 失败回退到 initial 草稿（标记"未经核验"）照常推送，核验是增强而非发布前置条件；opportunities/death_signals 这类衍生副产品的解析/写入失败绝不能阻断主报告推送。正则容错（代码块截断、大小写敏感）是真实漏洞但非主矛盾，应靠分层降级兜底而非寄望正则永不失败。

---

## 发现清单（按 P0 → P1 → P2）

### P0

**[P0] 非原子全量重写，崩溃即丢全库**（发现 1）
`src/chat_daily_tg/db.py:80-84, 151-153`（涉及 `repeat_topics.py:91-95`、`hot_leads.py:137-139`）
`_rewrite`/`mark_status`/`mark_lead_status` 直接 `open(path,'w')` truncate 后逐行写，无临时文件+rename，无 fsync。
触发：launchd kill（ExitTimeOut/重启）、Mac 休眠唤醒打断、磁盘满、SIGKILL 恰好发生在写到一半 → 整库丢失或只剩前 N 行，无备份可恢复。
修复：三处统一改为写同目录临时文件 → `f.flush()+os.fsync(fileno())` → `os.replace(tmp, path)` 原子替换。

**[P0] channels plist 直接调 .venv/bin/python 绕过守护脚本，.venv 消失时静默 exit 127 零告警**（发现 16）
`launchd/com.chat-daily-tg.channels.plist:13-20`
ProgramArguments 直接 `caffeinate -is .venv/bin/python run_daily.py --channels-only`，不经 `run_daily_guarded.sh` 的 .venv 预检+告警——正是 2026-06-12 修过的反模式在第二条入口原样重挖。
触发：uv 修剪/升级 cpython 软链或 .venv 被删，channels job 每天 8 次被拉起，python 不存在 → launchctl 在 Python 启动前 exit 127，`run_channels()`/`notify_failure()` 根本没机会执行。唯一痕迹是 channels-stderr.log 一行报错。
修复：新增 `scripts/run_channels_guarded.sh` 复用守护逻辑（.venv 预检 + osascript + curl Telegram 告警），plist 改走 `bin/cdrun-bash run_channels_guarded.sh`。

**[P0] 验证阶段解析失败丢弃已成功的初稿，当天零产出**（发现 34）
`src/chat_daily_tg/summarizer.py:128, 142-151, 230-239`
initial 草稿第 128 行已成功解析并完整在内存，但第二轮 verifier 解析+repair 都失败时第 239 行裸 raise，把可用的 initial 整份丢弃。核验本是增强步骤却成了发布硬前置。
触发：首轮成功，但 verifier 返回四-fence 输出畸形（缺第 4 个 fence/JSON 非法）且其 repair 也失败 → 内存中有完整报告却整份丢弃 → 当天零产出（仅失败通知）。verifier 几乎每次生产运行都执行，触发真实可达。
修复：在 `run_summary` 第 151 行 return 处包 try/except，verifier 解析失败时 log.warning 并回退返回 initial（附 `verification={'error':'verifier_parse_failed'}`），让未核验草稿照常推送。

> 注：编排维度的"TG 推送失败 catch-up 重跑重复入库"原报为 P0，验证阶段拆分为 hot_leads 盲 append（发现 40，P1）等具体条目；其中"hot_leads `{date}-hot-{i:03d}` id 重复污染"这一 P0 主张被驳回（该 id 生成规则在 src/ 中不存在，且 append_day_leads 生产侧无调用者）——详见末尾驳回清单。

### P1

**[P1] 混合相册逐项发送：部分 item 永久丢失且整帖标 seen 不补发**（发现 13，novel）
`src/chat_daily_tg/private_media.py:102-114, 172-181`
mixed-type album 走 `_send_media` 逐项发送，单 item 重试 3 次耗尽后被循环吞掉（只 log+继续）；只要 `ok>0` 不 raise，外层视为成功并把整帖所有 msg_id 写入 seen。
触发：一帖含 5 个混合媒体，第 3 个持续 429/413 → item 3 被吞、整帖标 seen → 该媒体附件永久不达，下次增量因 seen 不补发（文本 caption 仍随首个成功 item 送达）。
修复：`_send_media` 在有任一失败时回传失败信息；`push_private_channel` 仅在全部 item 成功时写 seen，否则不写让下次增量重试。

**[P1] 生产 launchd 不注入代理变量，Python 全链路依赖 Shadowrocket TUN**（发现 14）
`scripts/run_daily_guarded.sh:14-19, 78`（确切硬依赖是 `api.telegram.org`）
plist 只注入 PATH，wrapper 仅给告警 curl 用 `--proxy`，启动 Python 的第 78 行不 export HTTP(S)_PROXY；所有 httpx.Client 未传 proxy 也未设 trust_env=False。
触发：用户退出 Shadowrocket 或切非全局 TUN 模式后到次日 6:30，TG 推送 httpx 超时 → 日报不达（告警 curl 因独立 `--proxy` 仍可发，正报丢失）。
修复：第 78 行前 `export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" NO_PROXY=127.0.0.1,localhost`（放行本地 qwenproxy:3000），或构造 httpx.Client 时显式传 proxy。

**[P1] 频道转发器失败只发 macOS 通知不发 Telegram**（发现 19）
`src/chat_daily_tg/notifier.py:5-11`
`notify_failure` 只做 osascript，没有 wrapper 里那条经 1082 代理发 TG 的兜底；channels plist 又不经 wrapper。
触发：频道转发 Python 跑起来但内部失败（1082 挂、API 超时、配置异常），Mac 合盖睡眠时 osascript 不显示也不排队 → 用户永远不知道频道停了。
修复：`notify_failure` 内加 best-effort Telegram 兜底（读 .env 的 TG token/chat_id + 经 1082 代理 sendMessage），失败静默 swallow。

**[P1] kabi-tg-cli python 失效致所有私有频道集体失败时 run_channels 仍返回 0 零告警**（发现 20）
`src/chat_daily_tg/raw_channels.py:191-201`
私有频道 `dump_channel` 用硬编码 kabi-tg-cli python 跑 subprocess，RuntimeError 被 per-channel try/except 吞掉只 log.warning，total=0，`run_channels` 返回 0。
触发：uv 升级/修剪 cpython-3.13.5（kabi python 与 .venv 同源软链，一次 uv 清理同时打断两条链），每个私有频道 subprocess 启动即失败 → 频道转发静默归零（当天最高产源科技圈频道也归零）。
修复：`dump_channel` 开头校验 `os.access(TG_CLI_PYTHON, os.X_OK)`；`push_raw_channel_cards` 统计连续失败频道数，topic 组内全部私有频道失败则向上抛出/调 `notify_failure`。

**[P1] death_signals 用 LLM 文本 target 当键，title 重复时静默命中错误条目**（发现 6，novel）
`src/chat_daily_tg/death_signals.py:27, 31, 44, 51`
`title_to_id` 是 `{title: id}` 字典，同名 title 后者覆盖前者；死亡信号按 LLM 自由文本 target 查表命中"最后一个同名条目"。
触发：两个不同机会（不同 URL/分类）LLM 给了相同 title（标题漂移、泛标题、跨群同名）→ death signal 把状态打到错误条目，仍有效的机会被标 dead 或失效的逃过标记。无任何歧义检测。范围有界（仅写 dead/likely_dead、可恢复）但静默+无检测维持 P1。
修复：title→ids 改 `dict[str, list[str]]`，多于一个 id 时拒绝按 title 匹配（只允许精确 id）并 log.warning；优先要求 LLM 返回 id，title 仅作兜底且歧义时报警。

**[P1] compute_fingerprint 把 URL query 整段并入指纹，同一活动带不同 utm 产生重复条目**（发现 3）
`src/chat_daily_tg/db.py:18-35`
`_normalize` 只删非 `\w` 字符但保留 query key/value 文本，`?id=5` / `?id=5&utm_source=tg` 归一化后各不相同 → 同一机会被存多条。
触发：同一活动链接在不同群带不同 utm/share token 转发（IM 分享极常见）→ permanent.md 同一机会多行、mention_count 永停在 1、death 信号只命中其一。
修复：`compute_fingerprint` 内对 url 先用 `urllib.parse.urlsplit` 拆 scheme+host+path、丢弃 query/fragment（或仅剥离 utm_*/from/share* 跟踪参数、保留承载身份的 id）、lowercase host、去尾斜杠，再 `_normalize`。

**[P1] LLMClient 不处理 200 OK 但畸形/缺字段的 JSON 响应，直接崩管道**（发现 10，novel）
`src/chat_daily_tg/llm_client.py:50-51`
`r.json()` 与 `data["choices"][0]["message"]["content"]` 在 retry 块外；代理返回 200+HTML 错误页或 `{"error":...}` 无 choices 时抛 JSONDecodeError/KeyError/IndexError，不在捕获的 (HTTPStatusError, TransportError) 内，不重试直接中断（有 notify_failure 告警，故 P1 非 P0）。
触发：DeepSeek/qwenproxy 网关 200+错误页或限流软失败 → summarizer 拿不到 content 就先崩在 llm_client。
修复：把 line 50-51 包进 try，将 (ValueError, KeyError, IndexError) 加入捕获并复用退避循环重试。

**[P1] repair 后二次解析裸调，repair 失败炸掉全天日报**（发现 33）
`src/chat_daily_tg/summarizer.py:218-227`
`_parse_or_repair_summary` 第 227 行裸调 `parse_summary_output(repaired)` 无 try/except；repair 输出仍畸形时 ValueError 冒泡到 main catch-all，当天所有群摘要全丢（原始输出已落盘 raw_path，故 P1 非 P0）。
触发：首轮输出畸形触发 repair，同一抽风 LLM repair 时再次返回不合规格式 → 整天零产出 + 同日全管道重跑。
修复：第 227 行包 try/except ValueError，失败时对 repair 前 content 做尽力提取，能拿到非空 concise 就降级返回，只有连 concise 都提不出才 raise。

**[P1] concise 正文内合法代码块被非贪婪 fence 正则截断，内容静默丢失**（发现 37，novel）
`src/chat_daily_tg/summarizer.py:21, 26-35, 89`
`_FENCE_RE` 用 `(.*?)\`\`\`` 非贪婪匹配，正文里群友贴的代码块（```python）会让 concise/detailed body 在第一个内部 ``` 处截断，后半段（含来源尾注）静默丢弃，不触发 repair，降级守卫（<100 字符）也未必触发。detailed 无任何长度限制，是更可靠的触发面。
触发：技术/AI 群里有人贴代码，LLM 忠实搬运进正文 → 交付产物被静默截断且无原文兜底。
修复：把 fence 解析从非贪婪正则改为行级状态机（按行扫描开/闭 fence、正确处理嵌套），或在 `_extract_fences` 只接受已知 (lang,tag) 白名单、非白名单 fence 视为正文。

**[P1] verifier 误判真信息为 removed 时，核验后输出整份替换初稿，真信息无可挽回**（发现 39）
`src/chat_daily_tg/summarizer.py:279-294, 362-369`
verifier system prompt 要求它直接重写/删除它认为无证据的 claim，`parse_verified_summary_output` 用 verifier 输出完全替换 initial，无对账无并集；fact-risk-report 只列 status 不保留被删 claim 全文，无法回灌。
触发：verifier hallucination 或对原文证据误判，把实际有支持的 claim 当"无证据实体补全"删除 → 该真信息当天彻底消失（DB 不损坏、archive 原始导出仍在，故 P1 非 P0）。
修复：将 verifier 定位为"仅标注+给降级建议"，保留 initial 正文为发布主体，verifier 只输出 checked_claims，代码侧据此做有据可查的最小改写并把被删原 claim 全文写入 fact-risk-report；至少记录改动前后 diff。

**[P1] death_signals confidence/target 为 null 时 AttributeError 在推送前炸管道**（发现 35，novel）
`src/chat_daily_tg/death_signals.py:34, 39`
`sig.get('confidence','low').lower()` 与 `sig.get(...,'').strip()`：LLM 把字段输出为 JSON null 时 `.get` 返回 None，`.lower()`/`.strip()` 抛 AttributeError，沿 run_daily.py:479 冒泡。崩溃在 concise.md 已写盘但 TG 推送之前（有 notify_failure 告警、可重跑自愈，故 P1 非 P0）。
触发：LLM 在 death_signals 里输出 `{"confidence": null}` 或 `{"target_title_or_id": null}`（偶发畸形）→ 报告生成却推不出去 + 同日重跑。
修复：第 34/39 行做 None 防护（`(sig.get('confidence') or 'low')` 后再判 isinstance str）；并在 run_daily.py:478-484 外层包 try/except 让死亡信号失败不阻断推送。

**[P1] TG 推送失败后 catch-up 重跑导致 hot_leads 重复入库**（发现 40，novel）
`run_daily.py:433-531`（根因 `hot_leads.py:59`）
机会持久化（permanent/hot_leads/repeat_topics）在 TG 推送之前且无幂等门，推送失败不写 marker，catch-up 重跑再次写入。其中只有 repeat_topics 真幂等（seen_date 去重）、permanent 走 fingerprint 合并（仅 mention_count 虚增），**hot_leads 是盲 append 无任何 id 去重**，重复行穿透进 latest.md 与喂给 LLM 的上下文。
触发：6:30 持久化完成后 521 行 tg.send() 因 1082 代理失败抛异常 → 未写 marker → 9:00 catch-up 重跑 → 同批 hot_leads 再次 append（重跑 LLM 非确定，还可能产生 id 冲突）。
修复：`append_day_leads` 改为按 lead.id 去重的 upsert（读现有 jsonl→按 id 合并→整体重写）；或在持久化前用独立 `.persisted/date` 标记做幂等门。

**[P1] 长摘要多 chunk 推送非原子，中途失败重跑导致用户收到重复前半段**（发现 42，novel）
`src/chat_daily_tg/tg_sender.py:233-246`
`send()` 把 >3900 字符摘要切多 chunk 顺序发，chunk1 成功 chunk2 失败即抛异常；marker 不写，catch-up 重跑从 chunk1 重发。
触发：concise 经 HTML 转义后超 3900 字符（多群日报日常可达），chunk2 因代理瞬断/429 耗尽重试 → 9:00 重跑两 chunk 全重发。
修复：send() 内逐 chunk 记录已成功 message_id，失败时不整体回滚；或落盘 `.pushed` 标记让重跑只补缺失部分；最简：收紧 concise 长度上限让日报不分片。

**[P1] 派生视图重生成在持久化后无失败隔离，单条坏行炸全管道并跳过推送**（发现 43，novel）
`run_daily.py:487-488`（根因 `hot_leads.py:70-76` load_all_leads 无 per-line 容错）
`regenerate_permanent_md`/`regenerate_latest` 紧接持久化、推送之前无 try/except；任一历史 hot-leads jsonl 含坏行，`load_all_leads` 的 `json.loads`/`HotLead(**)` 抛异常即中断 _run，摘要已生成却不推送，marker 不写 → catch-up 反复在同一坏行炸掉，形成自我强化失败循环。
触发：某历史 DD.jsonl 含半行/缺字段（曾被中断写）→ 此后每日投递全部失效需人工清坏行。
修复：`load_all_leads` 循环体改 per-line try/except（捕获 JSONDecodeError/TypeError/ValueError）跳过坏行；或给 487-488 包 try/except 仅 log，使派生视图失败不阻断推送。

**[P1] read_all 对损坏/半截行无容错，一次崩溃后整库不可读**（发现 2）
`src/chat_daily_tg/db.py:72-78`（涉及 `repeat_topics.py:89`、`hot_leads.py:75`）
每行 `json.loads` 无 try/except，非原子 _rewrite 留下的截断尾行会让此后每次 read 抛 JSONDecodeError，库不可用直到人工修复（有顶层告警+只损坏尾行，故 P1 非 P0）。
触发：发现 1 的非原子写被打断后下次 run read_all 崩，持续中断。
修复：read 循环 try/except json.JSONDecodeError 跳过坏行并 log.error；配合发现 1 的 atomic rename 根除坏行来源。三个 read 函数统一加。

**[P1] notify_failure 把含 TG bot token 的 httpx 异常原文推到 macOS 通知**（发现 30，novel）
`src/chat_daily_tg/notifier.py:5-11`
RedactingFormatter 只挂 logging handler；notify_failure 直接 osascript 显示 `f"{type(e).__name__}: {e}"`，httpx.HTTPStatusError 字符串含 `https://api.telegram.org/bot<TOKEN>/sendMessage`，token 未脱敏进入通知横幅与通知中心历史。
触发：TG 推送失败 re-raise 的 HTTPStatusError 冒泡到 run_daily.py:89 → osascript 弹窗显示完整 token（截屏/共享屏幕/通知历史即泄露）。
修复：把 logging_setup 的 `_TOKEN_RE` 提为公开 `redact(text)` 函数，run_daily.py 三处 notify_failure 调用前统一过一遍。

**[P1] deploy.sh 用错误 launchd label，部署后静默跳过 plist 重载**（发现 17，novel）
`deploy.sh:17-19, 58-65`
`PLIST_LABEL="com.chat-daily.tg"` 与实际 label（com.chat-daily-tg.agent/channels）不符，对应模板文件不存在，命中 else 分支打印警告就跳过 reload，仍打印"✅ 部署完成"。定时 fork 的 job 会自动读新 .py，故纯代码改动生效；但 **plist 本身的改动（interval/参数/代理 env）永不被应用**，属配置漂移+部署假成功（故 P1 非 P0）。
修复：删掉自制 plist 逻辑，改调 `scripts/install-launchd.sh` 并补 channels 安装步骤；或至少修正 label 并对 agent+channels 各 cp+render+reload。

**[P1] deploy.sh 在 feature 分支上 git reset --hard origin/master，丢弃在途未提交改动**（发现 18，novel）
`deploy.sh:24-25, 40-41`
`BRANCH="master"` 硬编码，deploy/--pull 无条件 `git reset --hard origin/master`，无确认无 stash。已提交内容在 origin 可 reflog 找回，但**运行 deploy 那一刻工作区的未提交编辑会被无 reflog 抹除**（故 P1 非 P0）。
触发：在 channel-forwarder-and-reliability 分支带在途编辑跑 deploy → 编辑静默消失。
修复：`BRANCH="$(git rev-parse --abbrev-ref HEAD)"`；reset 前加守卫 `if ! git diff --quiet || ! git diff --cached --quiet; then echo '有未提交改动，已中止'; exit 1; fi`。

**[P1] deploy.sh 依赖安装用裸 pip（不在 PATH）被 || true 吞掉，部署后 venv 永不更新**（发现 21，novel）
`deploy.sh:52-55`
`pip install ... || true`，本机 pip 不在 PATH 且脚本未激活 .venv，命令整体被吞成 no-op 却仍打印成功；即便改 pip3 也会装进 Homebrew Python 3.14 而非 uv 管理的 3.13.5 venv。
触发：拉到引入新依赖的提交 → venv 缺依赖，下次 run 时 ImportError（channels 路径直接静默崩）。
修复：改 `uv sync`，失败 `exit 1`，去掉 `|| true`。

**[P1] channels plist 无任何安装脚本，重装/换机后频道转发静默缺失**（发现 22，novel）
`scripts/install-launchd.sh:14-16, 36-48`
install 脚本只渲染并加载 agent，完全没有 channels；channels job 当前是手动 load 的，且模板带 REPLACE_WITH_* 占位符必须渲染。
触发：新机/重装后跑 install-launchd.sh，只装日报 agent 无任何报错 → 用户以为装好了，频道转发从此不跑。
修复：把渲染+load 逻辑抽成函数，对 agent 和 channels 各调一次。

### P2

**[P2] launchd 重入对 JSONL 无文件锁，并发读-改-写丢更新**（发现 4）— `run_daily.py:82-83, 454-475`：休眠唤醒致 6:30 慢 run 与 9:00 catch-up 重叠时 lost update。修复：`_run` 入口或 guarded.sh 用 flock 非阻塞独占锁，拿不到就 exit 0。

**[P2] apply_death_signals 每信号一次全量 read+rewrite**（发现 5，novel）— `death_signals.py:33-49`：N 信号 N 次 truncate-重写，O(N×行数) 冗余 IO。修复：加 `mark_status_many` 批量收集后单次原子写。

**[P2] hot_leads append_day_leads 整段重写 md 但 jsonl 是 append，二者发散**（发现 7，novel）— `hot_leads.py:56-61`：同日重跑 md 只剩最后一批、jsonl 累积全部且无去重。修复：append 前按 id 去重，再读回当天全部 jsonl 重生成 md。

**[P2] SeenStore.add 写失败后无回滚**（发现 8，novel）— `raw_seen.py:46-52`：内存已加但磁盘 append 失败/进程被杀 → max_msg_id 回退重复推送（属可接受的"宁可重发不丢"）。修复：文件写失败时 `seen.discard(key)` 回滚或抛出让上层重试。

**[P2] LLMClient 对所有 HTTPStatusError 盲目重试，含 400/401/413**（发现 9）— `llm_client.py:57-67`：4xx 确定性失败仍退避 ~20s 后抛同一错（污染日志、延迟失败）。修复：4xx 非 429 直接 raise，只对 429/5xx/TransportError 退避。tg_sender.py 同模式可一并审视。

**[P2] _send_one 不处理 429，忽略 Telegram Retry-After**（发现 11）— `tg_sender.py:207-231`：与同文件其他三个发送方法不一致，低频日报路径偶发漏推一次。修复：加 429 分支用 `_retry_after(r)`。

**[P2] _retry_after 把 429 等待硬截到 30s**（发现 12）— `tg_sender.py:76-81`：长 flood-wait（60s+）下重试必再 429 放弃；但 seen-store 重试兜底使其变成延迟投递±2h 而非丢失。修复：上限放宽到 120-300s 或遵守服务器时长。

**[P2] VisionClient 无重试、单图抖动即丢该图**（发现 15，novel）— `vision.py:59-63, 100-103`：`except Exception: continue` 静默跳过该图（仅 INFO 级计数下降）。修复：加有限重试；except 至少 log.warning。

**[P2] cross_group_cluster O(n²) pairwise SequenceMatcher**（发现 26）— `cross_group_cluster.py:123-134`：topic 互不相似时退化全 O(n²)，n=800 实测 ~15s。修复：预归一化缓存 + 长度/前缀粗筛 + 候选总数上限。

**[P2] 每次 LLM/TG 调用新建 httpx.Client，无连接复用**（发现 27）— `llm_client.py:43-48`：低频管道下轻微浪费。修复：持有长生命周期 client 复用 keep-alive。

**[P2] evidence_index 暴力向量搜索 12 次重复反序列化**（发现 25）— 见 FP 第 2 节，numpy 批量化。

**[P2] send_media 429 退避时整文件重开重传**（发现 28，novel）— `tg_sender.py:369-389`：大媒体限流下最多 2 次冗余整体读盘上传。修复：循环前读入 bytes 一次复用或 `fh.seek(0)`。

**[P2] 子进程继承全部 API key**（发现 29）— `env.py:26`：5 处 subprocess 无 `env=`，kabi-tg-cli/Chrome 等白拿全部密钥（同 UID 本可直读 .env，故降为硬化级）。修复：subprocess 传最小白名单 env，或 load_env_file 不写 os.environ 改返回 dict。

**[P2] 聊天归档与日志 0644 落盘（PII at rest）**（发现 31，novel）— `paths.py:5-14`：archive/logs 0644 而 .env 是 600，自相矛盾。修复：进程入口 `os.umask(0o077)` 或对 DATA_DIR/产物 chmod。

**[P2] card_renderer Chrome 超时时不删含日报全文的临时 HTML**（发现 32，novel）— `card_renderer.py:229-247`：TimeoutExpired 时 unlink 不执行（NamedTemporaryFile 默认 0600）。修复：unlink 放进 finally。

**[P2] private_media subprocess 600s 超时 + 硬编码 uv 路径无前置校验**（发现 24）— `private_media.py:29-32, 54-58`：路径失效时 errno 埋在 stderr，大 backlog 单频道吃满 600s。修复：开头 `os.access` 校验给明确提示；超时下调到 ~180s。

**[P2] 日志同时写永不轮转的 launchd stderr.log**（发现 23，novel）— `logging_setup.py:24-30`：实测约 1MB/年增长（远非"吃满磁盘"）。修复：去掉 StreamHandler 或加每周清理 `find logs -mtime +30 -delete`。

**[P2] permanent 机会重跑 mention_count 虚增**（发现 41，novel）— `db.py:86-93`：`_merge_one` 无条件 +1，但该字段无下游消费（"重复=可信"判断实际在 repeat_topics）。修复：按 captured_at 日期幂等。

**[P2] wx/telegram 单群导出失败被静默吞掉无聚合告警**（发现 44，novel）— `run_daily.py:240-242, 268-270`：某群长期失败时日报安静缺席。修复：累计 failed_groups，超阈值或必达群失败则 notify_failure。

**[P2] --date 指定历史日因 active_*summary 用 date.today() 截断而上下文窗口错位**（发现 45，novel）— `run_daily.py:328-332, 488`：补跑 >retention_days 前的日，hot_lead 刚写入即被过滤。修复：cutoff 基准从 `date.today()` 改为传入的 date_str。

---

## 修复优先级路线图

### P0 立即修（数据丢失 / 静默故障，本周内）
1. **三处 _rewrite 加原子写**（发现 1）：write-temp + fsync + os.replace。这是 data-integrity 维度的总开关，同时根除发现 2 的坏行来源。
2. **新建 run_channels_guarded.sh，channels plist 改走守护 wrapper**（发现 16）：消灭第二条入口的 exit-127 静默故障，连带为发现 19/20 的频道告警铺路。
3. **summarizer 验证阶段失败回退 initial 草稿**（发现 34）：一个 try/except 防止"手握完好报告却整天零产出"。

### P1 本次修（管道中断 / 功能失效 / 安全泄露，本迭代内）
- **编排原子性**：持久化前加 `.persisted/date` 幂等门 + `append_day_leads` 按 id 去重（发现 40），send() 落 `.pushed` 标记或收紧 concise 长度（发现 42），派生视图重生成包 try/except + load_all_leads per-line 容错（发现 43/2）。
- **LLM 解析分层降级**：repair 二次解析加兜底（发现 33），death_signals None 防护 + 外层 try/except（发现 35），fence 解析改行级状态机（发现 37），verifier 改"仅标注不重写"（发现 39），llm_client 200 软错误纳入重试（发现 10）。
- **数据正确性**：compute_fingerprint 剥离 utm/规范化 URL（发现 3），death_signals title 歧义检测 + 优先用 id（发现 6）。
- **可靠性/告警**：guarded.sh export 代理 env（发现 14），notify_failure 加 TG 兜底（发现 19）+ token 脱敏（发现 30），kabi python 失效聚合告警（发现 20），混合相册部分失败不写 seen（发现 13）。
- **部署脚本**：修 label 走 install-launchd.sh（发现 17），BRANCH 改当前分支 + 未提交守卫（发现 18），改 uv sync 去掉 || true（发现 21），install 脚本补 channels（发现 22）。

### P2 记录待修（健壮性 / 性能 / 硬化 / 卫生，机会性处理）
- **性能批量化**（随数据增长才显现）：evidence_index numpy 批量（发现 25）、cross_group 预归一化+粗筛（发现 26）、httpx client 复用（发现 27）、death_signals 批量写（发现 5）。
- **429/重试健壮性**：_send_one 加 429 分支（发现 11）、_retry_after 放宽上限（发现 12）、4xx 不重试（发现 9）、VisionClient 加重试（发现 15）、send_media 不重读盘（发现 28）。
- **安全硬化**：subprocess 最小 env（发现 29）、umask 0o077（发现 31）、临时 HTML finally 清理（发现 32）。
- **运维卫生 / 边角**：flock 防重入（发现 4）、stderr.log 轮转（发现 23）、kabi 路径前置校验+降超时（发现 24）、SeenStore 回滚（发现 8）、hot_leads md/jsonl 对齐（发现 7）、mention_count 幂等（发现 41）、单群失败聚合告警（发现 44）、--date 历史补跑窗口对齐（发现 45）。

---

## 已驳回 / 低可信发现

- **deploy.sh --status 用错误 label 永远误报未加载**（deploy.sh:30）— 驳回。grep 默认 BRE 中 `.` 是通配符，模式 `com.chat-daily.tg` 里的 `.` 恰好匹配真实 label 的连字符 `-`，真机验证两行全命中、实际正确报告"已加载"。label 字面不一致是潜在脆弱性但未产生所述故障。
- **raw_seen.txt 无限增长致 max_msg_id 昂贵扫描**（raw_seen.py:20-41）— 驳回（性能侧）。代码确实 append-only 永不裁剪，但真机实测约 37 行/天，"几个月数十万行"实际需 15-20 年；即便 10 万行扫描也仅几十毫秒。属理论可能、实际不可达的非问题。
- **SeenStore.add 每条 key 单独 open+write 高频 fsync 级 I/O**（raw_seen.py:46-52）— 驳回。代码无 os.fsync（close 不强制刷盘），且每卡后有 1s sleep + 一次 HTTPS 往返，整循环已限速到 ~1 卡/秒，微秒级本地 append 完全可忽略，定性错误。
- **DEEPSEEK/GOOGLE/VISION key 经正常 log 路径泄露**（logging_setup.py:11-17）— 驳回。三个 key 都走 header 而非 URL，httpx 异常 repr 从不序列化 header，全代码无打印 r.text/headers/payload。只覆盖 TG token 的窄正则恰好够用；真正该补的是 notify_failure（已另列为发现 30）。
- **hot_leads 盲 append 重跑产生同 id 重复污染下游（定为 P0）**（hot_leads.py:59-61）— 驳回该 P0 定性。声称的 `f'{date_str}-hot-{i:03d}'` id 生成规则在 src/ 中根本不存在（凭空虚构），且 `append_day_leads` 在生产侧无调用者（仅测试调用）。append 去重缺失的真实隐患已由发现 7/40 以正确严重级别覆盖。
