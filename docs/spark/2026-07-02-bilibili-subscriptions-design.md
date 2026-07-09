# ChatDaily Bilibili 订阅 Digest 设计文档

## 1. 背景与问题

用户希望减少打开 B 站的次数。当前刷 B 站的主要动机是首页推荐/短视频，但"检查关注 UP 主是否更新"仍是打开 B 站的合理借口之一。如果能把关注 UP 主的新视频主动推送到 Telegram 群 topic，可以消除这个借口，减少不必要的打开频次。

## 2. 目标

在 ChatDaily 现有架构中新增 Bilibili 关注订阅源，以固定频率 digest 形式推送新视频到 Telegram 论坛 topic，并附带 AI 一句话摘要，帮助用户在不打开 B 站的前提下判断是否需要观看。

## 3. 需求范围

### 3.1 In Scope

- 拉取 B 站登录账号的关注 UP 主动态里的新视频
- 去重：只推送自上次运行以来新增的视频
- 每 4/6/8 小时生成一次 digest
- Digest 格式：富媒体卡片 + AI 一句话摘要
- 推送到 Telegram 群里的专用 topic（`bilibili`）
- 配置支持：Cookie/登录态、频率、白名单/黑名单、摘要开关

### 3.2 Out of Scope（本阶段不做）

- B 站直播开播提醒
- 非视频动态（文字、转发、投票）推送
- 自动根据观看行为推荐/筛选 UP 主
- 弹幕/评论拉取
- 多账号 B 站支持

### 3.3 未来可能扩展

- 根据 digest 被点击/已读数据自动生成"高价值 UP 主"白名单
- 支持 UP 主分组（科技/学习/娱乐），不同组推不同 topic

## 4. 方案核心思路

把 Bilibili 作为 ChatDaily 的第三个 source type（与 wechat、telegram 并列），通过 opencli 拉取关注 UP 主动态，经去重、AI 摘要后，以富媒体 digest 形式推送到 Telegram 论坛 topic，复用现有 `TelegramSender`、topic 路由表、`launchd` 调度与 guardian 脚本。

## 5. 架构与数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ChatDaily                                    │
│  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐   │
│  │ sources.wechat  │  │ sources.telegram │  │ sources.bilibili │   │
│  └────────┬────────┘  └────────┬─────────┘  └────────┬─────────┘   │
│           │                    │                     │              │
│           └────────────────────┴─────────────────────┘              │
│                                  │                                  │
│                          run_daily.py                               │
│                                  │                                  │
│                    ┌─────────────┴──────────────┐                   │
│                    │      bilibili_fetcher      │                   │
│                    │  - login / cookie refresh  │                   │
│                    │  - fetch following updates │                   │
│                    │  - dedup via SeenStore     │                   │
│                    └─────────────┬──────────────┘                   │
│                                  │                                  │
│                    ┌─────────────┴──────────────┐                   │
│                    │    bilibili_digest.py      │                   │
│                    │  - group videos            │                   │
│                    │  - LLM one-line summary    │                   │
│                    └─────────────┬──────────────┘                   │
│                                  │                                  │
│                    ┌─────────────┴──────────────┐                   │
│                    │       TelegramSender       │                   │
│                    │  - send_photo / media_group│                   │
│                    └─────────────┬──────────────┘                   │
│                                  │                                  │
│                         ~/.tg-notify-targets.json                   │
│                                  │                                  │
│                              Telegram Group                         │
│                            topic: bilibili                          │
└─────────────────────────────────────────────────────────────────────┘
```

## 6. 配置设计

在 `~/chat-daily/config.yaml` 中新增 `sources.bilibili` 段（以下为**已实现**的
实际 schema；调度频率不在 config 里，由 launchd plist 决定，见 §11）：

```yaml
sources:
  bilibili:
    enabled: true
    # 登录态由 opencli 管理；首次使用前先运行 `opencli bilibili login`
    # 后续 opencli 会复用本地 Chrome session，无需手动维护 Cookie
    opencli:
      profile: null                # 可选，opencli --profile
      timeout_seconds: 60          # 单条 opencli 命令超时

    fetch:
      # 白名单以 uid（mid）为 key —— B 站昵称可随时更改且不唯一，用昵称匹配
      # 会在 UP 改名后静默漏推；name 字段仅作注释，匹配逻辑不得使用。
      # 真实名单（Tier 1 共 23 个 UP 主）见运行时文件
      # ~/chat-daily/bilibili-whitelist.yaml（个人订阅数据，不进仓库）。
      whitelist:
        - {uid: 1000001, name: <UP主A>}
        - {uid: 1000002, name: <UP主B>}
      blacklist: []                # 始终排除的 UP 主（同样按 uid）
      max_per_digest: 30           # 单次 digest 最多视频条数，超出按时间取最新
      # 回看窗口。bvid 去重集合让重叠拉取零成本，取 48h 是为了容错：
      # 某次运行失败/机器休眠错过调度时，下次运行仍能追回漏掉的视频
      # （频道转发器为同类问题专门加过 6:00 catch-up，这里靠大窗口+去重解决）
      lookback_hours: 48
      per_up_limit: 8              # 每个 UP 拉最近几条（user-videos --limit）

    digest:
      topic: "bilibili"            # 对应 ~/qwenproxy/.tg-notify-targets.json 的 topic key
      summary_enabled: true        # AI 一句话摘要
      cover_enabled: true          # 是否发送视频封面
      link_enabled: true           # 是否附带 B 站视频链接
      card_delay_seconds: 1.0      # 卡片间隔，尊重 TG 限流
```

摘要模型直接复用全局 `models.vision`（qwenproxy 封面图理解），vision 不可用时
降级到 `models.summary` 文本 LLM 做元数据摘要，再失败则发无摘要卡片——不单设
`summary_model` 配置。Tier 2 完整视频摘要（下载视频喂多模态）**本次未实现**，
保留为未来扩展：封面+标题+简介的一句话摘要已满足"决定看不看"的需求。

在 `~/qwenproxy/.tg-notify-targets.json` 中新增 topic（真实 chat_id / thread_id 只存在
运行时文件里，本仓库文档一律用占位符，遵循已有的 scrub 惯例）：

```json
{
  "chat_id": -100REDACTED,
  "alert_thread_id": 1,
  "topics": {
    "alert": 1,
    "chat_daily": 2,
    "channels_news": 3,
    "channels_gallery": 4,
    "bilibili": 5
  }
}
```

## 7. 模块设计

### 7.1 新增文件

| 文件 | 职责 |
|---|---|
| `src/chat_daily_tg/bilibili_fetcher.py` | 调用 `opencli bilibili` 命令拉取关注动态/视频元数据；解析并去重 |
| `src/chat_daily_tg/bilibili_digest.py` | 组装 digest、调用 LLM 生成一句话摘要、渲染卡片 HTML |
| `src/chat_daily_tg/opencli_runner.py`（可选） | 封装 `opencli` 子进程调用、JSON 输出解析、超时/重试 |

去重状态**不新建模块**：复用 `raw_seen.SeenStore`（频道转发器已在用的
append-only 逐行 key 存储），key 形如 `bilibili:<bvid>`，路径新增
`BILIBILI_SEEN_PATH`。理由见 §12；不引入第三种 ad-hoc 状态格式
（本分支正在做 JSONL→SQLite 迁移，新增 JSON 状态文件与方向相悖）。

### 7.2 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/chat_daily_tg/config.py` | 新增 `BilibiliSource`、`BilibiliAuth`、`BilibiliFetch`、`BilibiliDigest` 配置模型；`Sources` 加入 `bilibili` |
| `run_daily.py` | 新增 `--bilibili-only` 入口与 `_run_bilibili()` 调度函数；解析 `resolve_tg_target("bilibili")` |
| `src/chat_daily_tg/paths.py` | 新增 `BILIBILI_SEEN_PATH` |

### 7.3 复用文件（不修改，仅调用）

- `src/chat_daily_tg/tg_sender.py`：发送富媒体卡片
- `src/chat_daily_tg/notifier.py`：失败告警
- `src/chat_daily_tg/raw_seen.py`：`SeenStore` 做 bvid 去重（见 §12）
- `scripts/guard_common.sh`：guardian 包装

## 8. B 站数据获取策略（双 transport，默认 API 直连）

`sources.bilibili.transport` 二选一（2026-07-02 晚新增 api 模式并设为默认）：

- **`api`（默认）**：httpx 直连 B站 Web API。实测确认 medialist 接口
  （`x/v2/medialist/resource/list?type=1&biz_id=<mid>`）**零 cookie、零 WBI
  签名**即返回单 UP 最新视频的全部所需字段（bvid/标题/封面/精确 unix 发布时
  间/秒级时长/mid/播放数）；简介对新视频经同样零 cookie 的 view API 补齐。
  无浏览器、无登录态维护，可在 Mac 或 r4s 无头运行；实测比 opencli 路径快
  ~15 倍（3 UP：4s vs 59s）。注意：**B站请求必须 `trust_env=False` 直连**——
  guard 导出的 HTTPS_PROXY 会把请求送到海外出口，正好触发 -352 风控（实测
  `x/space/wbi/arc/search` 即使带 WBI 签名 + buvid 也 -352，必须登录态；
  medialist 无此问题，这是选它的原因）。
- **`opencli`（fallback）**：原 Chrome-bridge 路径，medialist 未文档化、将来
  收紧时可一键切回。`opencli` 管理 B 站登录态和 Chrome session。

两条路径共享 uid 白名单、SeenStore 去重、lookback 过滤、排序与 digest 上限；
任一路径**全部 UP 失败会 raise 告警**（transport 挂掉不允许静默零推送）。

可用命令：

| opencli 命令 | 用途 | 本阶段使用 |
|---|---|---|
| `opencli bilibili login` | 首次登录，建立本地 session | 一次性使用 |
| `opencli bilibili user-videos <uid>` | 获取指定 UP 主最新视频 | **主要数据源**（逐个轮询白名单 uid） |
| `opencli bilibili video <bvid>` | 获取视频元数据（标题、作者+mid、封面、时长、精确发布时间、简介） | 补充视频详情 |
| `opencli bilibili following` | 获取关注列表 | 用于 whitelist uid 核实（一次性） |
| `opencli bilibili feed` | 获取关注动态 feed | **弃用**（见下） |
| `opencli bilibili summary <bvid>` | 获取 B 站自带的视频 AI 摘要 | **不建议依赖**，多数视频无官方摘要 |

> **实现偏离（2026-07-02 实测后定案）**：原设计以 `feed` 为主数据源，但实测
> feed 输出只有 `author`（昵称）/`title`/`likes`/相对时间/`url`——**没有 uid、
> 没有封面、没有精确时间戳**，uid 白名单无法作用于它（用昵称匹配正是 §6 明确
> 禁止的）。因此主路径改为逐个轮询白名单 UP 的 `user-videos <uid>`：uid 由查询
> 参数天然确定。23 个 UP × 24 次/天（每小时调度）≈ 550 次串行调用，走本地
> 登录态 Chrome bridge、不并发；若触发 B 站限流，降频回 4-6 小时而非绕过。

### 8.1 实际实现方式（`bilibili_fetcher.py`）

1. 对每个白名单 uid 调用 `opencli bilibili user-videos <uid> --limit 8 -f json --window background`（launchd 无前台 Chrome 场景必须带 `--window background`）；单个 UP 失败只记日志，不影响其余 UP。
2. 从 `url` 提取 bvid；与 SeenStore 去重；按 `date`（天粒度）粗筛 lookback 窗口。
3. 对每个候选 bvid 调用 `opencli bilibili video <bvid>` 补全封面、时长、简介、UP mid 和**精确发布时间**，再按精确时间做二次过滤。
4. AI 摘要分层：
   - **Tier 1（默认，已实现）**：`models.vision`（qwenproxy）分析**封面图 + 标题 + 简介**，成本低、速度快
   - **Tier 3（fallback，已实现）**：vision 不可用时，用 `models.summary` 文本 LLM 基于元数据生成摘要；再失败发无摘要卡片
   - **Tier 2（未实现，未来扩展）**：`opencli bilibili download` 下载视频后做完整视频理解，仅对精选 UP 启用

### 8.2 qwenproxy 可行性验证

测试视频：`BV1B9Tu6kEVh`《小米YU7 标准版能跑多远？》

| 测试项 | 命令/方式 | 结果 |
|---|---|---|
| 元数据获取 | `opencli bilibili video BV1B9Tu6kEVh` | 成功，获取标题、UP主、时长、封面、描述 |
| 封面图理解 | qwenproxy `qwen3.7-plus` + image_url | 成功，准确识别为续航测试 |
| 完整视频理解 | qwenproxy `qwen3.7-plus` + 37.7MB base64 视频 | 成功，准确总结核心结论 |
| B站官方摘要 | `opencli bilibili summary BV1B9Tu6kEVh` | 失败，返回 `EMPTY_RESULT` |

结论：
- qwenproxy 可以胜任多模态视频摘要，且理解准确。
- 不应依赖 B 站官方 `summary` 命令，因为多数视频没有官方摘要。
- 完整视频理解成本较高（上传大文件、处理时间长），建议只对白名单/精选 UP 主启用。

### 8.3 冷环境前置检查（launchd 场景）

opencli 依赖本地 daemon + Chrome/Browser Bridge。launchd 在 0:00/6:00 触发时
Chrome 可能未运行、机器可能刚从休眠唤醒——这与本分支刚为 WeChat 修过的
"cold-daemon export"是同一类问题（commit fc4c681），登录态过期只是失败模式之一，
**bridge/Chrome 不在才是 launchd 下更常见的失败**。处理：

1. 入口走 `scripts/guard_common.sh` 包装（与频道转发器一致），失败自动告警。
2. 抓取前先跑 `opencli doctor` 探活：daemon / extension / connectivity 任一不通，
   向 `alert` topic 发告警（区分"bridge 不可用"与"登录态过期"两种提示语）并退出；
   靠 48h lookback + bvid 去重，下次运行自动追回本次漏掉的视频。
3. 所有 `opencli bilibili` 命令统一带 `--window background`。
4. **待实测**：Chrome 完全退出状态下 `opencli bilibili user-videos
   --window background` 能否成功（列入 §14 验证项；不通过则需先解决 bridge
   冷启动，探活告警在此之前兜底）。

### 8.4 Fallback 策略

- 若 `user-videos` 因登录态失效返回错误，提示用户重新运行 `opencli bilibili login`，并向 `alert` topic 发送告警。
- 若 qwenproxy 不可用，回退到 ChatDaily 配置的文本 LLM 做元数据摘要（已实现）。
- 若 opencli 不可用，再考虑 RSSHub 或 B 站 Web API。

## 9. 默认订阅白名单选择逻辑

基于用户当前 96 个 B 站关注，第一阶段采用 **whitelist 模式**，只推送**信息密度高、观看决策成本大**的 UP 主。默认白名单共 **23 个 UP 主**，覆盖科技数码、AI/编程、财经商业、汽车、运动健康、效率工具、摄影摄像、美食、深度对谈等领域。

### 9.1 纳入 Tier 1 的标准

- 内容有明确信息价值（评测、分析、知识、教程）
- 视频时长通常 > 3 分钟，需要摘要帮助决策
- 不是纯娱乐、解压、短视频或生活 vlog（这类内容不适合"看摘要决定看不看"）

### 9.2 默认 Tier 1 列表（23 个）

真实名单（uid + 昵称注释）不进仓库，存放于运行时文件
`~/chat-daily/bilibili-whitelist.yaml`（2026-07-02 已通过
`opencli bilibili following` 逐一核实 uid，23/23 全部匹配）。分类分布：

| 分类 | 数量 |
|---|---|
| 科技/数码 | 5 |
| AI/编程 | 3 |
| 财经/商业 | 4 |
| 汽车 | 2 |
| 运动健康 | 5 |
| 效率工具 | 1 |
| 摄影摄像 | 1 |
| 美食 | 1 |
| 其他深度 | 1 |

### 9.3 明确排除的 UP 主类型

- 纯娱乐/搞笑、解压类
- 穿搭/生活 vlog
- 动画/连载
- 已停更或 3 个月以上未更新的大部分账号
- 经用户明确要求移除的账号（具体名单见运行时文件的筛选记录）

### 9.4 后续扩展

运行 1-2 周后，根据 digest 的点击/已读情况和实际观看行为，可逐步将 Tier 2（其他信息类 UP 主）加入白名单，或把 Tier 1 中低价值的移出。

## 10. Digest 卡片格式

每条视频卡片包含：

- 封面图（下载后随 sendPhoto 上传）
- 标题（HTML 加粗）
- UP 主名 / 时长 / 发布时间 / 播放数
- AI 一句话摘要
- **「▶️ 在 B 站观看」inline-keyboard 按钮**（可选，`link_enabled` 控制）——
  链接不进 caption 文本，按钮点击区域大、跳转更方便

示例（caption HTML + 按钮）：

```html
<b>【标题】</b>
👤 UP主名 · 12m34s · 07-02 08:00 · 45,615播放
📝 一句话摘要：讲解了新一季的动画制作幕后...
```
```
[ ▶️ 在 B 站观看 ]   ← reply_markup inline button
```

发送方式：`TelegramSender.send_photo(cover, caption, button=(text, url))`；
封面缺失/发送失败时降级 `send_card(caption, link=url, button=…)`（文本卡片
带链接预览 + 同一个按钮）。

## 11. 调度（已实现：`launchd/com.chat-daily-tg.bilibili.plist`）

独立 plist `com.chat-daily-tg.bilibili`，走 `scripts/run_bilibili_guarded.sh`
守护包装（venv preflight + 告警），**每小时一次**（2026-07-02 由 6 小时调整）。
**取 :30 偏移**避开频道转发器（6:00/12:00/18:00 等整点）的调度碰撞——两个 job 同时唤醒会争抢 opencli
daemon / tg-cli / 代理：

```xml
<key>StartCalendarInterval</key>
<array>
  <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
</array>
```

手动运行命令：

```bash
uv run python run_daily.py --bilibili-only [--no-push]
```

## 12. 状态与去重

**决策：不新建 JSON 状态文件，复用 `raw_seen.SeenStore`**（append-only、
逐行 key、成功发送后才写入——crash 时下次重试而非丢失，语义与频道转发器
完全一致）。理由：本分支正在做 JSONL→SQLite 迁移，再引入一种 ad-hoc JSON
状态格式与方向相悖；`pushed_bvids` 的"已推送集合"语义与 SeenStore 天然吻合。

状态文件：`~/chat-daily/bilibili_seen.txt`（`paths.BILIBILI_SEEN_PATH`），
key 形如 `bilibili:<bvid>`。

去重逻辑：

1. 拉取关注动态中最近 `lookback_hours`（48）小时的视频。
2. 过滤掉 SeenStore 中已存在的 bvid。
3. 每条卡片**成功发送后**立即把 bvid 追加进 SeenStore。
4. 不做过期清理：23 个 UP 主的量级下一年也只有数千行，append-only 即可；
   `last_check_at` 字段取消，回看窗口每次运行从当前时间倒推。

## 13. 错误处理

- **opencli daemon / Chrome bridge 不可用**（launchd 冷环境最常见）：`opencli doctor` 探活失败即告警退出，提示语区别于登录态过期；漏掉的视频靠 48h lookback 下次追回（见 §8.3）。
- **opencli 登录态过期**：向 `alert` topic 发送告警，提示运行 `opencli bilibili login`，停止本次运行。
- **opencli 命令失败/超时**：指数退避重试 3 次，仍失败则告警。
- **单个视频拉取失败（如封面图缺失）**：跳过该条，继续推送其他视频。
- **LLM 摘要失败**：回退到无摘要的富媒体卡片。

## 14. 验证方式（2026-07-02 实施记录）

1. **配置校验** ✅：`load_config(CONFIG_PATH)` 通过，23 个白名单 uid 全部解析。
2. **抓取测试** ✅：`run_daily.py --bilibili-only --no-push` 返回 48h 窗口内 5 条新视频，日志列出 bvid/标题/UP。
3. **发送测试** ✅：真实运行 5/5 卡片（封面 + vision 摘要 + 链接）到达 `bilibili` topic。
4. **去重测试** ✅：紧接着重跑返回 `new videos: 0`；seen 文件含 5 个 `bilibili:<bvid>` key。
5. **单元测试** ✅：fetcher/digest/config 共 24 个新用例，全套件 233 passed。
6. **冷启动测试 ⬜ 待做**：完全退出 Chrome 后运行 `opencli bilibili user-videos <uid> -f json --limit 3 --window background`，确认无前台浏览器时抓取仍可用；不通过时 `probe_bridge` 告警兜底（漏掉的视频下轮追回），但需修 bridge 冷启动才能真正无人值守。
7. **摘要模型核实** ✅：`models.vision`（qwen3.7-plus）实测生成摘要正常。

## 15. 风险与待决策

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| launchd 冷环境下 Chrome/bridge 不在 | 拉取失败 | `opencli doctor` 探活 + guard 包装告警；48h lookback 下次追回；实现前冷启动实测（§14.6） |
| opencli bilibili 登录态过期 | 拉取失败 | 通过 `opencli bilibili login` 重新登录；告警提示 |
| opencli 命令不稳定/超时 | 拉取失败 | 封装 runner 做超时、重试、fallback 到 user-videos |
| UP 主改昵称 | 白名单静默漏推 | 已消除：白名单按 uid 匹配，昵称仅注释 |
| 关注 UP 主过多导致 digest 过长 | 信息过载 | `max_per_digest` + 后续自动筛选 |
| 完整视频摘要成本过高 | LLM 费用/时间上升 | 只对白名单 UP 主启用；默认用封面图摘要 |
| 封面图或视频链接被 Telegram 屏蔽 | 卡片显示异常 | 纯文本 fallback |
| AI 摘要增加 LLM 成本 | 费用上升 | 摘要可关闭，或只给白名单 UP 主做摘要 |

## 16. 实现优先级

1. **P0**：配置模型 + bilibili_fetcher + 状态去重 + 基础 digest 推送
2. **P1**：AI 摘要 + 富媒体卡片
3. **P2**：白名单/黑名单 + `opencli` profile 配置
4. **P3**：观看数据统计与自动筛选

## 17. 结论

建议在 ChatDaily 内新增 `sources.bilibili` 模块，第一阶段即采用 **uid 白名单模式**（Tier 1 共 23 个 UP 主，名单在运行时文件），每小时推送一次富媒体 + AI 摘要 digest 到 Telegram 专用 topic。该方案复用现有 infra（TelegramSender、SeenStore、guard 脚本、launchd），实现成本最低，且后续可按 digest 反馈逐步调整白名单。

## 18. r4s 迁移评估（2026-07-02 实测）

背景：每小时经 opencli 开关 Chrome 页面在 Mac 上不优雅。建议路径分两步：
step 1 API 直连（已实现，§8），step 2 视稳定性迁 r4s。以下为 step 2 的实测评估。

### 18.1 环境实测结果

| 检查项 | r4s (FriendlyWrt, aarch64) | 结论 |
|---|---|---|
| Python | 3.11.14，pip3/opkg 可用，stdlib(ssl/sqlite3/urllib) ✓ | ✓ |
| cron / curl | ✓ | ✓ |
| 内存 / 磁盘 | 3.3G free / 根分区 668M free | ✓（磁盘紧但够） |
| B站 API 直连 | 200，0.19s（国内家宽 IP，风控最友好） | ✓ |
| Telegram 出口 | 直连不通（预期）；本机 clash :7890 监听中但**拒绝代理连接**（allow-lan/bind 配置限制，未擅自改动） | ✗ 待解 |
| Gemini 出口 | 同上（generativelanguage 需代理） | ✗ 待解 |
| 备选出口 | bwg 与 r4s 同 tailnet；bwg→api.telegram.org 直连 0.4s ✓ | 可作跳板 |

（bwg 承担 B站抓取不可行：海外机房 IP 触发 -352 风控，实测 arc/search 即是此错误码。）

### 18.2 Go / No-Go

**条件性 Go**：唯一 blocker 是 r4s 的出海通道，二选一解决即可迁移：

1. **修 clash 配置**（推荐，5 分钟）：`allow-lan: true` / `bind-address: '*'`
   使 127.0.0.1:7890 可用——需用户自行修改路由器配置（守则：不擅自动用户代理配置）。
2. **bwg 隧道**：r4s 上 `ssh -NL 7893:127.0.0.1:... bwg` 常驻（systemd/procd 守护）
   或 bwg 起 socks 绑 tailscale0，TG/Gemini 流量经 tailnet 走 bwg 出口。

### 18.3 迁移清单（出口解决后执行）

- [ ] 摘要切换：`models.vision` 由 qwenproxy(localhost:3000) 改为 Gemini 多模态
      （已实测可行：gemini-3.5-flash OpenAI 兼容端点 + base64 封面，5.6s/条，
      需调大 max_tokens 或关 thinking 防截断）——去掉最后一个 Mac 本机依赖
- [ ] r4s 部署：`pip3 install httpx pydantic pyyaml`（或打包 stdlib-only 精简版）；
      同步 `~/chat-daily/{config.yaml,.env}`、`bilibili_seen.txt`、
      `~/qwenproxy/.tg-notify-targets.json` 路由表
- [ ] cron：`30 * * * *`（对齐现 launchd :30 节奏）；HTTPS_PROXY 指向选定出口，
      B站请求已 trust_env=False 不受影响
- [ ] 告警通道：notify_failure 的 osascript 分支在 r4s 无效，TG 告警分支已够用
- [ ] 双跑一周：r4s cron 与 Mac launchd 并行（SeenStore 各自独立会重复推送——
      **切换日 Mac 侧先 unload launchd**，seen 文件拷过去做种）
- [ ] 观察 medialist 接口风控表现；若收紧，切回 Mac `transport: opencli` 过渡

### 18.4 结论与执行记录（2026-07-03 已迁移）

用户选定出口方案 2 的变体：**bwg 上跑 tinyproxy（EPEL 一次性启用安装），
绑定 tailscale IP `100.87.113.14:8888`、ACL 只放行 100.64.0.0/10、仅允许
CONNECT 443**——tailscale 本身就是加密隧道，无需 ssh 转发守护。实测
r4s→bwg→Telegram 302 / Gemini 可达。

迁移已按 18.3 清单执行完毕：
- 依赖：pip3 --user（清华镜像；musl wheel 全命中）+ tzdata
- 代码：git archive HEAD → `/root/chat-daily-tg`；notifier 补了 Linux 无
  osascript 的兼容（e2c656c）
- 摘要：r4s 侧 config 的 models.vision 改为 Gemini 多模态直连（经 bwg 出口）
- cron：`30 * * * *` 走 `scripts/run_bilibili_r4s.sh`（flock 防重叠、
  TZ=CST-8——musl 下命名时区静默回退 UTC 会重现 8h 偏差，必须用 POSIX 格式）
- 切换顺序：Mac launchd unload → seen 文件同步 → r4s cron 启用 → 受控真发
  验证（1/1 卡片 8s，含 Gemini 摘要与封面）
- Mac 侧 plist 保留未删，installer 中注释掉 bilibili label 防双跑；回滚 =
  r4s `crontab` 移除该行 + Mac `launchctl load`
