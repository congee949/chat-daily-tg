---
title: 定时任务心跳监控 + 看板 (task-monitor)
date: 2026-07-04
status: approved-design
topic: task-monitor-dashboard
machines: [macOS(本机), r4s(OpenWrt), bwg(Rocky Linux)]
---

# 定时任务心跳监控 + 看板 (task-monitor)

## 1. 背景与问题

三台机器(macOS 本机 / r4s / bwg)上跑着十几个定时任务(cron / systemd timer / launchd),
覆盖 chat-daily 三件套、CC98 系列、market-recap、x_monitor、日报等业务推送,以及
zju-connect / webvpn-keeper / backup 等保活任务。

现有告警是 **push(推送式)**:任务成功 → 发 TG;`run_*_guarded.sh` / `run_bilibili_r4s.sh`
在 `exit != 0` 时调 `guard_notify` / `alert` 发失败告警。

**根本盲区 —— 沉默失败(silent failure)**:任务自己不会告诉你"它没跑"。以下情况**不产生任何 TG 消息**:

- cron / launchd / timer 根本没触发(调度器挂了、条目被删、机器重启后没起来)
- 脚本在 `guard_notify` 之前就崩了(机器宕机、磁盘满、解释器缺失)
- 整台机器离线
- 任务"成功退出"但实际没干活(上游 API 持续返回空,静默降级)

于是"没收到消息"无法区分「今天没新内容」还是「任务已经死了三天」。
**程序没法提醒你它自己已经死了。**

## 2. 目标 / 非目标

**目标**
- G1 检测沉默失败:任务超过预期窗口没有成功心跳 → 主动 TG 告警(独立于任务本身)。
- G2 主动查看:一个只读网页看板,一眼看到每个任务的 最后成功 / 下次预计 / 状态灯 / 最近错误。
- G3 统一裸 cron 的失败告警:目前只有 chat-daily 三件套有失败告警,cc98/market-recap/x_monitor
  等裸 cron 失败是无声的;经 hb-wrap 后失败也会即时告警。
- G4 复用现有资产:告警通道、代理出口、TLS/tailscale、python server 范式,不引入新运行时。

**非目标**
- 不做常驻 daemon(tinyproxy/caddy/tailscale/openclash)的独立存活探测——它们挂了会通过
  承载任务的心跳超时**间接**暴露(YAGNI,第二套探测机制留待将来)。
- 不做指标/趋势/历史图表(不是 Prometheus/Grafana),只做"当前是否健康"。
- 不做告警分级/值班/升级路由,单一 alert topic 够用。
- 不上公网,不做多用户/权限。

## 3. 已定决策(四轮确认的结论,固化)

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 监控范围 | A(业务推送) + B(保活)层**所有周期性任务**打心跳;常驻 daemon 不单独探测 |
| D2 | 心跳采集 | 调度层包 `hb-wrap`,按 **exit code** 精确打心跳(成功/失败/没跑) |
| D3 | 中心 | **bwg** 自写轻量 python `server.py`(仿 research-kanban 范式) |
| D4 | 传输 | hb-wrap 成功/失败后 **HTTP POST** 到 bwg 中心(tailscale 内网) |
| D5 | 存储 | **SQLite**(中心本地) |
| D6 | watchdog | bwg **systemd timer(5min)**,读 SQLite 判超时/失败,复用 alert topic + tinyproxy |
| D7 | 看板 | 中心渲染只读 HTML,**绑 tailscale IP,零认证** |
| D8 | 兜底 | macOS 每日 launchd job ping 中心 `/health`,不通 → osascript 本地弹窗 |

## 4. 架构总览

```
┌─ 采集层 (每台机) ────────────────────────────────────────────┐
│  cron/timer/launchd → hb-wrap <name> -- <原命令>            │
│                         │  跑原命令, 拿 exit code           │
│                         │  fail-open POST 心跳 (绝不拖累任务) │
└─────────────────────────┼───────────────────────────────────┘
                          │  HTTP POST (tailscale 内网)
                          ▼
┌─ 中心层 (bwg, /opt/task-monitor) ───────────────────────────┐
│  server.py  绑 100.87.113.14:8900                           │
│   ├─ POST /hb/<task>?status=ok|fail&exit=<n>   → SQLite     │
│   ├─ GET  /            → 看板 HTML (读 SQLite)              │
│   └─ GET  /health      → 200 (供 mac 兜底探活)             │
│                                                             │
│  watchdog.py  (systemd timer, 每5min)                       │
│   └─ 读 SQLite → 算健康态 → 状态跳变时 TG 告警(去重)         │
└─────────────────────────┬───────────────────────────────────┘
                          │ 复用 guard_notify 同款通道
                          ▼
      .tg-notify-targets.json (chat_id + topics.alert)
      → 经 tinyproxy(100.87.113.14:8888) 发 TG alert topic

┌─ 兜底 (macOS) ──────────────────────────────────────────────┐
│  launchd 每日 → curl /health, 不通 → osascript 本地弹窗      │
└─────────────────────────────────────────────────────────────┘
```

**数据流一句话**:`任务成功/失败 → hb-wrap → POST /hb → SQLite → (看板读) / (watchdog 判超时 → 告警)`。

## 5. 组件详细设计

### 5.1 hb-wrap(采集层, POSIX sh, 一份分发三台机)

职责:包住原任务命令,执行它,按退出码上报一次心跳。**心跳上报必须 fail-open,绝不改变被监控任务的行为或退出码。**

```sh
#!/bin/sh
# hb-wrap <task-name> -- <command...>   —— 纯 POSIX sh (r4s ash / bwg+mac bash 通吃)
# 位置: r4s /root/bin/hb-wrap ; bwg+mac /usr/local/bin/hb-wrap
HB_CENTER="${HB_CENTER:-http://100.87.113.14:8900}"   # 各机可 override
name="$1"; shift; [ "$1" = "--" ] && shift
"$@"; rc=$?                                            # 跑原命令, 保留退出码
if [ "$rc" -eq 0 ]; then st=ok; else st=fail; fi
# 失败才带 log 尾; POSIX 命令替换, 不用 bash 的 <()
err=""; [ "$rc" -ne 0 ] && err=$(tail -c 200 "${HB_LOG:-/dev/null}" 2>/dev/null)
# fail-open: 心跳 POST 超时/失败一律吞掉, 不能影响 rc
# --noproxy '*': 强制内网直连, 不被任务 export 的 HTTPS_PROXY(tinyproxy) 劫持
curl -s --max-time 8 --noproxy '*' -X POST \
  "${HB_CENTER}/hb/${name}?status=${st}&exit=${rc}" \
  --data-urlencode "error=${err}" \
  >/dev/null 2>&1 || true
exit "$rc"                                             # 原样透传退出码
```

关键约束:
- `--max-time 8` + `|| true`:中心宕机或网络断,hb-wrap 也秒返,任务照常。
- 退出码原样透传:被 launchd/cron 认为的成败不变。
- **纯 POSIX sh**:r4s 是 OpenWrt ash,不能用 `<()`/`[[ ]]`/数组等 bashism。
- **`--noproxy '*'`**:hb-wrap 在原命令**之后**于父 shell 跑,通常继承 cron 干净环境;但 mac guard 路径与部分任务会 export `HTTPS_PROXY`,故显式禁代理确保直连 tailscale 中心。

### 5.2 中心 server.py(bwg, 绑 100.87.113.14:8900)

单文件 python3(stdlib `http.server` + `sqlite3`,仿 research-kanban,无第三方依赖)。三个端点:

- **`POST /hb/<task>?status=ok|fail&exit=<n>`**
  - 以 **服务端接收时刻** 作为时间戳(统一时钟,规避三台机时钟漂移 —— 见 §7)。
  - `status=ok` → 更新 `last_ok_ts`、`last_seen`、`last_status=ok`、`last_exit=0`、清 `last_error`。
  - `status=fail` → 更新 `last_seen`、`last_status=fail`、`last_exit`、`last_error`(POST 的 `error` 字段,截前 200 字符);**不刷新 `last_ok_ts`**。
  - 未在注册表中的 `<task>` → 记为 `unregistered` 收录展示,但**不参与告警**(提醒补注册)。
- **`GET /`** → 看板 HTML(§5.5)。
- **`GET /health`** → `200 OK`(仅探活,供 mac 兜底)。

### 5.3 SQLite schema

```sql
-- 注册表: 部署时按 §6 任务清单初始化 (亦可 server 启动时从 tasks.json seed)
CREATE TABLE tasks (
  name        TEXT PRIMARY KEY,   -- 与 hb-wrap <name> 一致
  machine     TEXT NOT NULL,      -- mac | r4s | bwg
  cadence     TEXT NOT NULL,      -- 人读节奏, 如 "每时:30"
  threshold_s INTEGER NOT NULL,   -- 超时阈值(秒)
  enabled     INTEGER DEFAULT 1
);
-- 运行状态: 每任务一行, 心跳更新
CREATE TABLE status (
  name        TEXT PRIMARY KEY REFERENCES tasks(name),
  last_seen   INTEGER,            -- 最近任一心跳(server 接收时刻, epoch)
  last_ok_ts  INTEGER,            -- 最近一次成功(server 接收时刻)
  last_status TEXT,               -- ok | fail
  last_exit   INTEGER,
  last_error  TEXT,
  alerted     INTEGER DEFAULT 0   -- 告警去重: 1=已就当前红态告过警
);
```

SQLite 开 WAL 模式,规避 watchdog 读与 POST 写的竞态。

### 5.4 任务注册表(完整清单 + 阈值 + 注入点)

阈值原则:周期任务 = 间隔 × 2~3;每日任务统一 **26h**(容忍单日跳过一次不误报,契合"失败/跳过下轮会补"的现有哲学)。

| name | 机器 | cron/schedule | 节奏 | 阈值 | hb-wrap 注入点 |
|---|---|---|---|---|---|
| daily | mac | launchd `com.chat-daily-tg.agent` | 每日≈8:00¹ | 26h | guard_common.sh 内 |
| channels | mac | launchd `com.chat-daily-tg.channels` | 每2h | 3h | guard_common.sh 内 |
| bilibili | r4s | `30 * * * *` | 每时:30 | 90min | cron 前缀(或脚本内成功路径) |
| cc98-signin | r4s | `30 7 * * *` | 每日7:30 | 26h | cron 前缀 |
| cc98-daily | r4s | `40 7 * * *` | 每日7:40 | 26h | cron 前缀 |
| cc98-want-watch | r4s | `*/30 * * * *` | 每30min | 75min | cron 前缀 |
| nexushd-signin | r4s | `35 7 * * *` | 每日7:35 | 26h | cron 前缀 |
| market-recap | r4s | `0 6 * * *` | 每日6:00 | 26h | cron 前缀 |
| zju-watchdog | r4s | `*/5 * * * *` | 每5min | 20min | cron 前缀 |
| webvpn-keeper | r4s | `0 * * * *` | 每时 | 90min | cron 前缀 |
| backup | r4s | `30 4 * * *` | 每日4:30 | 26h | cron 前缀 |
| x-monitor | bwg | `*/30 * * * *` | 每30min | 75min | cron 前缀 |
| macrumors | bwg | `0 8 * * *`(CST) | 每日8:00 | 26h | cron 前缀 |
| growth-digest | bwg | `10 8 * * *`(CST) | 每日8:10 | 26h | cron 前缀 |
| research-kanban-worker | bwg | systemd timer | 每2min | 15min | ExecStartPost + OnFailure |

¹ daily 触发时间以 `com.chat-daily-tg.agent.plist` 的 `StartCalendarInterval` 为准;26h 阈值不依赖精确时刻,部署时无需改。

**默认不纳入**(避免噪音,如需再加):`cc98 health_check`(5min 保活)、`zju-connect restart`(日4:02)、
系统级 `logrotate`/`dnf-makecache`/`tmpfiles-clean`、所有常驻 daemon。

### 5.5 看板(GET /, 绑 tailscale, 零认证)

一面墙,按机器分组,每任务一张卡:

- 状态灯:🟢 新鲜(`now - last_ok_ts < threshold`) / 🟡 接近超时(`> 80% threshold`) / 🔴 超时或 `last_status=fail` / ⚪ 从未上报(pending)
- 上次成功(相对时间"12min 前") + 下次预计 + 最近一条错误(若有)
- 顶部汇总徽章 `🟢12 🟡1 🔴2 ⚪0`
- `<meta http-equiv="refresh" content="30">` 自动刷新(零 JS 依赖,最简)
- 标题:「任务心跳看板」

### 5.6 watchdog.py(systemd timer, 每5min)

遍历 `tasks ⋈ status`,对每个 `enabled` 且非 `unregistered` 的任务算健康态,**只在状态跳变时告警**:

- `绿/黄 → 红`(`now - last_ok_ts > threshold` 或 `last_status=fail`)且 `alerted=0`
  → 发「⚠️ <name>@<machine> 已 Nh 没成功(last: <相对时间>, exit=<n>)」,置 `alerted=1`。
- `红 → 绿`(重新有新鲜成功心跳)且 `alerted=1`
  → 发「✅ <name>@<machine> 已恢复」,置 `alerted=0`。
- 持续红:`alerted=1` 不变 → **不重复刷屏**。
- `⚪ 从未上报`:部署初期给一次性宽限(见 §7),不当红处理。

告警发送复用 `guard_notify` 逻辑(移植其 python 分支到 watchdog):读 `/root/qwenproxy/.tg-notify-targets.json`
的 `chat_id` + `topics.alert`,TG_BOT_TOKEN 从环境/配置读,经 tinyproxy(`100.87.113.14:8888`)发送。

### 5.7 兜底:macOS 每日 ping(看守看守者)

中心/watchdog 自身在 bwg 单点。极简兜底:macOS 一个 launchd job(每日一次)
`curl --max-time 10 --noproxy '*' http://100.87.113.14:8900/health`,非 200 → `osascript` 本地弹窗
「⚠️ task-monitor 中心不可达」。零网络依赖的本地通知,利用"你每天在用 mac"。

## 6. 部署改动清单

1. **中心**(bwg `/opt/task-monitor/`):`server.py`、`watchdog.py`、`tasks.json`(注册表 seed)、
   `runtime.env`(TG_BOT_TOKEN 等)、systemd unit `task-monitor.service`(server 常驻)+
   `task-monitor-watchdog.service`/`.timer`(5min)。看板经现有 caddy 或直接绑 `100.87.113.14:8900`。
2. **hb-wrap 分发**:r4s `/root/bin/hb-wrap`、bwg + mac `/usr/local/bin/hb-wrap`。
3. **r4s / bwg 裸 cron**:`crontab -e` 每条纳入任务加 `hb-wrap <name> -- ` 前缀(保留原有 flock/env 不动)。
4. **macOS launchd**:在 `guard_common.sh` 增加 `guard_heartbeat` helper,`run_*_guarded.sh`
   成功/失败路径各调一次(复用已有 proxy/env,不引 hb-wrap)。
5. **bwg systemd timer**:`research-kanban-worker.service` 加 `ExecStartPost`(成功打 ok)+
   `OnFailure=`(失败打 fail);或同样用 hb-wrap 包 `ExecStart`。
6. **兜底**:macOS 新增 `com.task-monitor.watchdog-ping.plist`(每日)。

## 7. 错误处理与边界(对抗式审查)

| 场景 | 处理 |
|---|---|
| **心跳上报失败拖累任务** | hb-wrap `--max-time 8` + `|| true` + 原样透传 exit code。心跳是尽力而为,**永不改变任务成败**。 |
| **中心宕机** | POST 全失败但 fail-open,任务照跑;watchdog 也停 → 靠 §5.7 macOS 兜底发现。 |
| **心跳被 HTTPS_PROXY 劫持** | r4s 任务 / mac guard 会 export tinyproxy 到 `HTTPS_PROXY`,其 `NO_PROXY` 不含 100.x → curl 一律加 `--noproxy '*'` 强制直连中心。 |
| **hb-wrap 遇 bashism 失败** | r4s 是 OpenWrt ash,脚本纯 POSIX(无 `<()`/`[[ ]]`/数组),shebang `#!/bin/sh`。 |
| **单次心跳丢包** | 阈值 = 间隔×2~3,单次丢失在下个周期补上即不超阈,不误报。 |
| **三台机时钟漂移** | 时间戳一律用**服务端接收时刻**,不信任务端时间,单一时钟源。 |
| **任务改名/新增** | 注册表(tasks.json)是唯一真相;hb-wrap `<name>` 必须与之一致。未注册 task 记 `unregistered`,展示但不告警,提醒补注册。 |
| **首次部署无历史** | `last_ok_ts` 为空 → ⚪pending,不告警;部署后每任务手动触发一轮打底,或给注册后 1×阈值 的初始宽限。 |
| **持续红刷屏** | `alerted` 标志去重,一次红态只告一次,恢复才复位。 |
| **watchdog 与 POST 竞态** | SQLite WAL + 短事务。 |
| **每日任务因时区/DST 晚跑** | 26h 阈值宽于 24h,足够容忍。 |
| **中心重启** | SQLite 落盘,状态与 `alerted` 不丢,不会重启后重复告警。 |
| **macOS 合盖休眠** | mac 侧 daily/channels 心跳与兜底 ping 可能延迟——已知局限,mac 监控可靠性弱于 VPS,阈值已放宽(daily 26h),不追求 mac 实时性。 |

## 8. 测试与验收

- **hb-wrap 单元**:mock `exit 0` / `exit 1` 命令 → 验证 POST 的 `status`/`exit` 正确;中心不可达时任务退出码不变(fail-open)。
- **watchdog 逻辑**:造"last_ok 超阈值"记录 → 验证告警触发一次;再跑一次 → 不重复(去重);写入新鲜成功 → 验证恢复告警。
- **端到端**:手动触发一个真任务 → 看板对应卡变🟢;停掉一个任务等过阈值 → 收到 TG 告警;恢复 → 收到恢复告警。
- **兜底**:停中心 server → mac 每日 ping 应弹本地通知;`/health` 恢复后不再弹。
- **看板**:tailscale 内打开 `http://100.87.113.14:8900/` 显示全量任务与正确状态灯;非 tailscale 网络访问不通(验证零暴露)。

## 9. 实施顺序(建议分阶段)

1. **中心骨架**:server.py(三端点)+ SQLite schema + tasks.json seed;本地 curl 打桩心跳验证存储。
2. **hb-wrap + 一个任务打通**:先给 bilibili(r4s)接入,端到端验证 POST→看板变绿。
3. **看板**:GET / 渲染 + 状态灯 + 30s 刷新。
4. **watchdog + 告警**:systemd timer + 状态机 + 去重 + 复用 alert topic;造超时验证告警。
5. **铺开所有任务**:按 §5.4 注册表逐台接入 hb-wrap 前缀 / guard_heartbeat / systemd。
6. **兜底**:macOS 每日 ping。

## 10. 开放问题 / 未来增强

- (增强)常驻 daemon 存活探测第二套机制(tinyproxy/caddy/tunnel)——本期不做。
- (增强)看板加"手动重跑"按钮(触发远程任务)——越权到执行层,本期只读。
- (增强)心跳历史/成功率趋势——本期只存当前态。
- (确认项)`daily` 的精确触发时刻部署时读 plist 核对(不影响 26h 阈值)。
