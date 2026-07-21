# Implementation Notes — yihong / 科技圈 personalize + 09:00 slot

## Design Decisions

### yihong：日签仪式整帖 drop
- **选择**：对起床日签、城市打卡、LeetCode 日题、早安股市全部 `exclude_patterns` 整帖丢弃；只保留干货外链。
- **理由**：这些是固定模板的日常仪式帖，与已有 digest / 用户偏好重叠或无信息密度；`prefer_content_link: true` 继续服务有价值的外链帖。
- **锚点**：起床用 `(?m)^今天的起床时间是`（正文锚点）替代原先 `(?m)^#morning\s*$`，更稳；`(?m)^#morning\b` 仅作旧标签兜底。

### 科技圈：留在 channels_news
- **选择**：不新建 topic、不做激进 exclude；只加注释标明定位为私密媒体流、走资讯栏。
- **理由**：内容本身就是资讯流目标，拆 topic 增加路由与维护成本，当前无个性化过滤需求。

### 09:00 channels 补档
- **选择**：在 06:00 与 10:00 之间插入 09:00，共 9 档（6/9/10/12/14/16/18/20/22）。
- **理由**：原先 06→10 有 4h 空窗，早间是资讯高峰；+0–15min jitter 与 grace_s=4500 不变。
- **联动**：`schedule.yaml`（事实源）→ `schedule.py apply` → launchd；`task-monitor/tasks.json` hours/cadence 同步，避免监控误报。

## Deviations
- 无。按批准计划落盘。

## Tradeoffs
- 09:00 与 growth 09:30 接近：channels 有 jitter 最多拖到 ~09:15，growth 在 09:30，仍错开；不合并、不挪 growth。
- yihong exclude 用子串/行首 regex，可能误伤正文碰巧含「早安打工人」等短语的帖——概率极低，可接受。

## Open Questions
- 无。
