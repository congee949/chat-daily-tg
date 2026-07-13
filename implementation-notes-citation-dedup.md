# Implementation Notes — citation dedup & push idempotency (2026-07-13)

背景：2026-07-12 日报里同一张「苹果起诉 OpenAI」文章截图在 今日总览 和 AI/工具
两处各内联一次（`[IMG1]` 被 LLM 打了两遍）。归档扫描证实 8 天中 3 天（07-09 /
07-10 / 07-12）有重复 marker，属模式性问题。本次修复覆盖 blindspot pass 列出的
P1 全部 + P2 三项。

## Design Decisions

- **去重放在代码层（resolve_citations），prompt 约束只作辅助**。LLM 已证明会违反
  格式约定（8 天 3 犯），与 codebase 其他 LLM 输出 backstop（coerce_enum、
  _extract_fences）保持同一原则：LLM 产出的结构必须有 code-level 兜底。
- **去重的保留偏好：分栏 > AI/工具 > 文档序，总览垫底**。总览是索引，图片跟着
  分栏详情走（与 07-02 用户偏好「插图应为 AI 相关」同向）。仅在总览出现的引用
  仍保留（不弃图）。
- **去重先于 max_images 截断**，重复 marker 不再消耗引用预算（07-10 的重复
  [IMG4] 曾挤掉一张独特图）。
- **推送幂等用 day-level 阶段 marker**（`.card-sent` / `.digest-sent`），不做
  内容 hash：catch-up 重跑会重新生成不同文本，语义上「当天日报最多送达一次」
  与内容无关。与既有 COMPLETE/PERSISTED marker 模式一致。
- **句读换位**（`[IMG1]。`→`。[IMG1]`、`[IMG1]（电丸）`→`（电丸）[IMG1]`）在
  分段前做，治截图里图片下方的孤儿「。」；括号尾注限 ≤40 字符无换行，避免吞正文。

## Deviations

- 无 spec；以 blindspot pass 报告为准。全部 P1 项按报告实现。

## Tradeoffs

- **timeout 重试的 at-least-once 重复未修**：Bot API 无幂等 token；「超时视为
  已送达」会换成丢报风险（比重复更糟）。接受现状，`.digest-sent` 已把 catch-up
  级别的整报重发堵住。
- **跨平台同图（不同文件、内容相同）未做感知去重**：需要 pHash 类新依赖；
  build_citation_block 已按 local_path 去重挡住同文件双 id。留作 P2。
- **`.digest-sent` 写在 trailing photos 之前**：文本送达后崩溃则重跑不补发照片
  （照片本就 per-photo failure-isolated、best-effort），换取正文绝不重发。
- **unknown-id 剥除现在连同左侧空格一起吃**（原行为留双空格）：更新了既有测试
  期望，属有意的观感改进。

## Adversarial Review（Orca → grok 异源评审）

无 P0。5 个 P1 全部修复：

- P1-1 stale `.card-sent` × image_only 组合可能静默空天 → skip 条件加 `send_image` 门。
- P1-2 catch-up 卡片晚于正文送达（顺序倒置）→ `.digest-sent` 存在时抑制晚到卡片。
- P1-3 半角括号尾注仍孤儿 → 换位正则加 `\([^()\n]{1,40}\)` 分支（嵌套全角括号
  留作 P2，LLM 实际输出中未见）。
- P1-4 测试缺口 → 新增 5 个测试（非 AI 分栏 vs 总览偏好、半角尾注、同路径保最高分、
  `.digest-sent` 抑制晚卡 e2e、空 citation_map 剥 marker e2e），修正一处错误注释。
- P1-5 同路径去重保首见不保最高分 → 改为按 value_score 保最优。

评审 P2 残留（接受，不修）：`.digest-sent` 先于尾随照片写入（照片 best-effort）；
多 chunk 部分发送 + 重生成 hash 不匹配的前缀重复（pre-existing，review #42）；
相邻 marker、全角空格 pad、感知级同图去重。全文见评审报告（scratchpad，会话级）。

## Open Questions

- 无。若某天希望「仅总览引用」也强制下沉到分栏位，需要 marker 搬运（改写文本），
  当前只做保守的删除/保留。
