# Implementation Notes — vision 阶段可观测性 + 重试（2026-07-14）

背景：用户报告 07-14 早晨日报图片全部消失，怀疑是 5eb2f4a（引用去重）引入的回归。

## 诊断结论（先于改动）

07-14 零图与去重修复**无关**，因果链：

1. `archive/2026/07/13/vision.jsonl` 为 0 字节 → vision 阶段产出 0 条分析 → citation_map 为空 → 推送走无图 fallback（日志 "0/0 trailing cited image(s)"）。
2. 重放当天 top 候选图（真实端点）：模型给分 0.0–0.4，全部低于 0.8 入选线，且模型自投 `include=false`（表情包/水图日）。
3. vision 阶段耗时 219s（~22 张图 × ~7s）→ 调用大多成功，非全量失败。
4. 历史基线（grok 评审修正日期口径，按**报告日**计）：07-04/06/07/13 为 0 citable 天——零图天占近一半，是 0.8 门槛下的常态，只是用户此前未注意。（初判写的 07-03/05/07/08 是墙钟日期，口径混了。）
5. 去重修复本身在 07-10/07-12 归档回放中行为正确（保分栏、去总览）。

真正的问题：**管线无法自证**——"今天真没好图" 与 "vision 端点挂了" 在归档和日志里长得一模一样。

## Design Decisions

- **stats_out 出参而非改返回值**：`analyze_media_candidates` 返回类型保持 `list[VisionAnalysis]`，5 处测试调用与 run_daily 的 mock patch 均不受影响；计数通过可选 `stats_out: dict` 暴露。out-param 不算优雅，但把改动面压到最小。
- **VisionClient 重试 backoff 用 [2, 5] 而非 cfg.retry 的 [5, 15, 60]**：重试是按图执行的（每天 ~35 张），大 LLM 的退避表最坏加 ~45 分钟，不可接受；2s/5s 最坏加 ~4 分钟。故意不接 cfg.retry。
- **仅 429/5xx/网络错误重试，4xx 直接抛**：payload 过大、鉴权失败等重试不会自愈。
- **告警口径（grok 评审 P1-1 修正后）**：`attempted>0 且 api_failed>0 且 included==0` 即告警——"部分 429 + 其余低分"造成的静默零图对读者和全灭长得一样；有图入选时部分失败只留 WARNING，避免 CLIProxy 偶发限流噪音。
- **吸收的 grok 评审项**（异源对抗评审，无 P0，报告见 `.omo/grok-review-vision.md`）：P1-1 告警口径放宽；P1-2 新增 `vision-audit.jsonl` 全量留痕（含 api_failed 行）供 0.8 阈值事后校准；P1-3 `model_veto` 独立计数；P1-4 vision prompt 示例 `should_include_in_daily` 从 false 改 true 并明示"不要照抄示例值"（示例 false 会诱导模型系统性否决高分图）；P2-3 `_normalize_score` 支持百分制 rescale（85→0.85，旧行为归零）。
- **未采纳/留待观察**：Retry-After 头（固定 2s/5s 是有意取舍）；每图新建 httpx.Client（旧行为，非正确性问题）；`skipped_prefilter` 不再细拆低分/无 path。

## Deviations

- 无 spec，无既定计划偏离。0.8 入选线**未动**（2026-07-02 用户亲自从 0.65 调上来的），是否加"零图天自适应放宽"留作 open question。

## Tradeoffs

- per-image INFO 日志每天多 ~22 行——换来"为什么没图"一行可答，值得。
- vision.jsonl 保持"仅 included"语义不变；落选分析的留痕由本次新增的 `vision-audit.jsonl` 承担（见 Design Decisions 的 grok P1-2 项——本段早先写"audit 文件留待未来"，与后文矛盾，评审 sweep 抓出后已更正）。

## Open Questions

- **0.8 入选线是否保留**：~~待用户拍板~~ → **已决策（2026-07-14，用户选 b）**：0.8 线不动，零图天自适应保底——从 ≥0.65、未被模型否决的落选图中提升最高分一张（`fallback_min_score=0.65`）。被否决（model-veto）和 empty-filter 的图不参与保底。07-13 归档全量重放验证：22 attempted / 0 过线 → 保底提升 0.75 分 AI 教程截图，breakdown=`included=1 (fallback=1) below_bar=2 model_veto=7 filtered_empty=12 api_failed=0`。
- 留待后续（已挂后台任务卡片，用户已在独立会话启动）：TG 高产群按时间 cap 20 改按价值截断；`.digest-sent` 早于 trailing photos 写入的"有字无图"缺口。

## Review Fixes（PR #8 xhigh 审核后追加，2026-07-14）

多智能体审核（10 finder × 5 verifier × 1 sweep）报 15 条 findings，已修：

- **C1 保底压制告警**：健康判定改看"原生入选"（included − fallback_included），保底图不再掩盖大面积失败。
- **C2 阶段级异常静默**：外层 except 补 notify_failure。
- **C4 双口径分叉**：新增 `vision_zero_image_failure(stats)` 单一判定，ERROR 日志与 TG 告警共用。
- **C3 全跳过静默**：attempted=0 且 ≥10 张死于有效性门槛时判定为提取管线故障（阈值 `_INVALID_ALERT_THRESHOLD`）。
- **A1/A2 audit 文件**：抽为 `write_vision_audit`——无条件写入（空则截断旧文件）、`errors="backslashreplace"` 防代理字符炸档、移到 vision.jsonl 之后并独立 try（写失败绝不吞掉当日图片）。
- **R1 软 200 不重试**：choices 提取移入 `_post_with_retry`，except 拓宽为 (HTTPError, ValueError, KeyError, IndexError)，对齐 llm_client review #10 的教训。
- **R2 挂死 ×3**：连续 5 张图全失败即熔断（`_CIRCUIT_BREAKER_FAILURES`），跳过余量并计入 `aborted_early`。
- **S1 float(True)=1.0**：`_normalize_score` 入口 isinstance(bool) 拦截归零。
- **S4/sweep 百分制静默**：docstring 更正为 >100 归零，rescale 时打 WARNING。
- **X2 阈值进配置**：新增 `VisionModel` 配置类（min_prefilter_score/min_include_score/fallback_min_score），run_daily 显式传参，代码默认值保留。
- **sweep 引用脱节**：citation_map 非空但 LLM 零引用时打 WARNING，防"统计说有图、日报实际无图"的误归因。
- **sweep prompt 示例自相矛盾**：示例改为自洽（0.85+true）并明示两个字段都勿照抄。

**未修留档**（P2，含理由）：audit 双 schema（消费方按 decision 分支即可，暂无 in-repo 消费者）；stats snake_case 与 decision 连字符双词汇表（改动波及测试与已落盘归档，收益低）；weekly_media_rules_review 混计保底行（等 audit 数据积累后随校准一起做）；RETRYABLE_BACKOFF tuple 化、dict 解包次序、永久性传输错误重试（均为无实害加固）。
