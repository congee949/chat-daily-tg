# 实现笔记归档

**这些是冻结的历史记录，不是现行文档。**

每份笔记记的是**当时那个任务**的设计决策、对规格的偏离、取舍和当时的开放问题。它们写完就冻结，不随代码更新——所以里面的具体数值、路径和"待办"很可能已经过期。

要知道系统**现在**是什么样，看这三份：

| 你想知道 | 看 |
|---|---|
| 规则和红线 | 仓库根 `CLAUDE.md` |
| 系统怎么工作 | [../ARCHITECTURE.md](../ARCHITECTURE.md) |
| 出事了怎么办 | [../runbook.md](../runbook.md) |

这些笔记的**留存价值是"为什么"**——尤其是那些否决记录：某个看似显然的方案为什么行不通、某个反直觉的设计是踩了什么坑才变成现在这样。这类信息在正式文档里放不下，但能防止后人重走弯路。截至 2026-07-15，其中的常驻知识已全部吸收进上面三份文档。

## 清单

| 文件 | 时间 | 主题 |
|---|---|---|
| `implementation-notes.md` | 2026-06-02 ~ 07-03 | 主线累积：图片输出、频道原文卡片、私有频道媒体、2 小时增量转发、晨跑失败根因、vision 接入与后端迁移、相册折叠、微信图片下载、富消息内嵌图、launchd 代理污染 |
| `adversarial-review-2026-06-29.md` | 2026-06-29 | 深度对抗式审查报告：58 agents / 45 条存活发现（P0×4 / P1×19 / P2×22）。下面那份 review-fixes 是它的修复记录 |
| `implementation-notes-review-fixes.md` | 2026-06-29 | 对抗式审查修复：SQLite 迁移、LLM 解析分层降级、编排幂等、告警 |
| `implementation-notes-bilibili.md` | 2026-07-02 ~ 07-03 | B站订阅 digest：双 transport、风控止损、迁移 r4s |
| `implementation-notes-route-table.md` | 2026-07-09 | TG 统一路由表 + LLM 输出校验 |
| `implementation-notes-growth-mining.md` | 2026-07-11 | 成长内容挖掘：A/B 卡、judge 异源化、富文本升级 |
| `implementation-notes-citation-dedup.md` | 2026-07-13 | 引用图去重 + 推送阶段幂等 |
| `implementation-notes-vision-observability.md` | 2026-07-14 | vision 可观测性、重试、零图保底 |

## 已知的过期内容

不要照着这些执行：

- `implementation-notes.md`（2026-06-10 条目）说 `deploy.sh` "脱节且危险，提醒勿直接运行"——**已于 2026-06-29 修复**（`require_clean_tree` + detached-HEAD 守卫 + `uv sync`），现在可正常使用。
- `implementation-notes-route-table.md` 的路由表数值已漂移（如 `market_recap` 从 17 变为 1146）。**事实源是 `~/qwenproxy/.tg-notify-targets.json`**。
- 频道转发调度记为"08–22 共 8 次"，实际是 `6,10,12,…,22`（无 8:00）。
- `implementation-notes-review-fixes.md` 说 SQLite 是三张表，成长挖掘后来又加了三张，现为六张。
