# Spec: raw channel 内容级去重 + fming_weekly 接入

日期：2026-07-16
状态：**已实现**（content_seen.py + 57 测试，同日下午）；本文档为原始设计 + as-built 修订
修订：同日 blindspot pass（见 `2026-07-16-cross-producer-and-topic-dedup-design.md`）对本设计做了三处语料实证的解析修正与 guards 升级，as-built 差异见文末「实现修订」节
来源：用户提出接入 t.me/fming_weekly 前的调研；对该频道 112 帖（2026-02-15 → 07-16）与 yihong0618 频道本地库的交叉分析。

## 调研结论：fming_weekly

- **频道**：Frost's Notes（@fming_weekly），Frost Ming（PDM 作者）个人频道，1.32K 订阅，全量约 775 帖。
- **频率**：112 帖 / 151 天 ≈ **0.74 帖/天**，负担极小。
- **内容构成**：Python 生态（PDM/bub）、AI/agent 工具、开源社区观察、#share 链接分享约六成；生活杂谈约四分之一；其余 #blog / #game 等。
- **可以接入**：公开频道（有 username），tg-cli 用户会话 join 后即可 sync。帖子多为「短评 + 外链」，建议 `prefer_content_link: true`（同 yihong0618）。

## 问题：重复内容穿透现有幂等层

fming_weekly 是重转发型频道：112 帖中 **约 40 帖（36%）是转发**。其中 **6 帖转发自已订阅的「yihong0618 和朋友们的频道」**（≈1.2 帖/月），转发与原帖间隔 45 分钟到数天。已验证 fming_weekly/760 与 hyi0618 msg 13591（PyCon China 2026）**文字逐字相同**。

现有 `SeenStore` 只按 `(chat_id, msg_id)` 幂等——同一内容经转发进入另一频道后 msg_id 不同，**无论 A 转 B 还是 B 转 A，两张卡都会推送**。

**现有 4 频道基线**（本地库实测，2026-07-16）：LydiaPod / 科技圈在花 / 投机之路 / yihong0618 之间逐字互转 0 例；URL 级重复 2 例（06-27 同一篇公众号文章 9 分钟内被科技圈在花与 yihong0618 先后分享；06-05/06 Anthropic 文章被科技圈在花与 LydiaPod 隔天各自评论）。即当前重复率很低，**重复问题主要随 fming_weekly 这类重转发型频道的加入而出现**——且 fming 的转发来源（张晋涛、螺莉莉等）都在同一社交圈，未来每多订阅一个圈内频道，两两重叠组合数上升。

另外两点约束：

- **fwd 元数据不可用**：tg-cli messages.db 全库 69577 条消息 `raw_json` 均为空（`raw_channels.py` 的 fwd 检测实际是死代码），无法拿到 fwd_from 的源频道 id + 原 msg_id 做精确去重。
- **频道内隔天重发**：fming 样本中仅 2 例同 URL 重发，且都在 1.5 小时内（作者续帖/删重发），跨天重发未观察到；但用户预期的「一周内同内容再现」在跨频道场景真实存在。

→ 唯一可用信号是**消息文本本身**（含其中的 URL）。

## 设计：内容指纹 + 时间窗

### 新模块 `content_seen.py`

三个构件：

1. **`canonical_urls(text) -> set[str]`** — 提取正文外链并归一化（复用 `_URL_RE`）：
   - host 小写、去 `www.`；fxtwitter / vxtwitter / twitter / fixupx → `x.com`；
   - tweet 链接只保留 `x.com/status/<id>`（用户名、`?s=20&t=…` 跟踪参数全丢弃）；
   - bilibili 只保留 `BV` 号（丢 `share_source` / `vd_source`）;
   - 其余域名保守处理：只去 `utm_*` 参数，其它 query 保留（`?page=2` 可能是不同内容）。
2. **`text_fingerprint(text) -> str | None`** — 去 URL、去空白与标点、lowercase 后 sha1。归一化后 **< 24 字符返回 None**（「哭了」这类短帖不参与，防误杀）。
3. **`ContentSeenStore(path, window_days)`** — sqlite 单表 `fingerprint PRIMARY KEY, chat_id, msg_id, sent_at`，存 `~/chat-daily/state/content_seen.db`。加载时 prune 过期行；`busy_timeout=5000`（agent 与 channels 两个 label catch-up 时可能重叠）。

### 接入点：`push_raw_channel_cards` 发送循环

构卡后、发送前计算指纹（用 strip_promo_lines 后的 head content），命中判定分两级——**宁可重复，不可误杀**：

- **强命中 → 跳过**：文本指纹相同（转发原文场景，已实证逐字一致）；或 URL 指纹相同**且**本帖是裸链接帖（去 URL 后正文 < 30 字）。
- **弱命中 → 照发**：URL 相同但正文有实质评论（不同作者对同一 tweet 的不同评论各有价值）。卡片加 ♻️ 标注为 P2，第一版不做。

跳过的 msg_id 走 excluded_ids 同路径写 SeenStore（推进增量高水位），并 log 命中来源（哪个频道、几天前）。指纹注册 **write-after-send**，与 seen 一致的崩溃语义。整个判定 try/except 包裹：指纹层任何异常（含 store 打不开）→ 视为无命中照常投递（投递优先于完美）。

私有频道路径（private_media）：有 caption 的帖同样接入；纯媒体帖第一版不去重（未来可用媒体文件 sha1——该路径本来就下载文件）。

### 配置

```yaml
sources:
  telegram:
    dedup_window_days: 14   # 0 = 关闭
    raw_channels:
      - id: "<join 后从 tg-cli dialogs 获取>"
        name: "Frost's Notes"
        username: fming_weekly
        prefer_content_link: true
        # dedup: true 为默认，可按频道关闭
```

窗口默认 14 天（用户观察是「两三天到一周」，双倍留余量）。同一轮内频道按配置顺序注册指纹——**原创源排前、转发型排后**，yihong0618 应排在 fming_weekly 之前。

## 决策记录

- **否决 fwd 元数据方案**：raw_json 全空，除非改 tg-cli 的存储层，不值得为此扩大爆炸半径。
- **否决 embedding / 语义相似度**：观察到的重复是逐字转发，精确指纹已覆盖；语义层引入误杀风险与 LLM 依赖，违反「投递优先于完美」。
- **否决纯 URL 去重**：两个作者独立评论同一链接的帖各有价值，URL 命中只在裸链接帖时跳过。
- **store 用 sqlite 而非 SeenStore 式 append-only 文本**：时间窗语义需要 prune，append-only 文件做不了；项目已有 sqlite3 使用先例。

## 对抗式审查（设计阶段）

| 风险 | 缓解 |
|---|---|
| 短文本误杀（「哭了」） | 归一化后 <24 字不产生文本指纹 |
| URL 归一化过激吞掉不同内容 | 只对 twitter/bilibili 做激进归一化，通用域名仅去 utm_* |
| store 损坏阻断投递 | 判定层 try/except，异常一律视为无命中 |
| agent 与 channels label 并发写 | sqlite busy_timeout；两 label 常态错峰（6:00 / 6:30） |
| 发送失败却注册了指纹 → 下轮真内容被误跳 | write-after-send，与 SeenStore 同语义 |
| 窗口 prune 失效致 store 无限膨胀 | 每次加载时 DELETE 过期行；0.74 帖/天 × 5 频道量级可忽略 |

## 实现修订（as-built，2026-07-16 下午）

实现落在 `src/chat_daily_tg/content_seen.py` + `tests/test_content_seen.py`（57 测试）。与上文设计的差异，全部来自同日 blindspot pass 的语料实证（细节见 `2026-07-16-cross-producer-and-topic-dedup-design.md`）：

1. **URL 提取先解析 markdown 再扫裸链**：`[label](url)` 先消费（label 计入正文），残余文本的裸 URL 在全角标点/CJK 汉字处终止、尾部按括号配平剥离。直接动因：设计稿自己引用的 06-27 重复对（42209/13645）在原 `_URL_RE` + `rstrip(".,;!?\"'")` 规则下 canonical URL 对不上（尾随 `)` 与 `）` 存活）；语料另有 46 处中文正文胶连进 URL。该对已成为验收 fixture。
2. **裸链接阈值改为 ≤10 个实质码点**（unicodedata P/S/Z/C 之外的字符数），替代 "<30 字"——30 字符按英文校准，21 字中文已是完整策展评语。
3. **推文提取容 `/i/status/<id>`（无作者形态，语料主流）、`/photo/N` 子路径、`/statuses/` 旧形态**；article → `a:<id>` 键；t.co 语料 0 例，不解析。
4. **Guards 随「跳过」策略升级为必需**：抑制 journal（`~/chat-daily/state/dedup_journal.jsonl`，dedup_journal.py，L1/L2 共用）+ 日报页脚计数 + `--resend` 逃生口（集成时接线）。归档天然覆盖被抑制卡（archive 写在发送循环前）。
5. **禁用的缓解手段（显式记录）**：任何 defer-one-cycle 类方案——SeenStore 高水位是 `max(全部已见 id)`，被延后的 msg 永不再抓，纯静默丢失。同轮撞车明确接受（代价是几秒注意力）。
6. **XMonitorIndex（跨 producer 精确层）实现但休眠**：同日度量 NO-GO（6 天 0 命中，gate 预写 ≤1/月），集成不构造；fming 入组后复测可直接启用。
