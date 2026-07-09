---
title: AUQ4me 合并盲区分析（blindspot pass）
date: 2026-07-05
status: analysis
topic: auq4me-merge-blindspot
scope: task-monitor(/opt/task-monitor) + research-kanban(/opt/research-kanban) 合并为个人 AI infra
method: 6 路视角 finder → 逐条对抗核实（41 agents，37 条原始发现，32 存活/2 击杀）→ 完整性批判补 5 条 → 人工归并为 7 簇
inputs:
  - 2026-07-04-task-monitor-dashboard-design.md (spec)
  - project_kanban_bwg.md (迁移记忆)
  - bwg 实机代码快照 + 运行态清单 (2026-07-05)
---

# AUQ4me 合并盲区分析

## 一句话结论

**「合并两个看板」这个动词底下捂着至少五个互相独立的决策，每一个你都从未被迫回答过**——因为两套系统各自独立时，这些问题根本不存在。最大的盲区不是任何一条技术风险，而是：**你还没说清合并要消灭的痛点是什么**，而两个看板的使用哲学恰好是相反的。

## 零号问题：动机（先答这个，其他决策树都挂在它下面）

task-monitor 是**挂墙玻璃**：服务端渲染、零 JS、30s 整页自刷——零 JS 意味着页面不可能被脚本异常卡死，这是监控面的可靠性属性，不是审美选择。research-kanban 是**操作台**：20s JS 轮询 + 弹窗编辑 + 拖拽状态机，v2 曾实测一行 ReDoS 冻结整页 16 秒。

> **Q0：你想消灭的痛点具体是哪个——「手机上要记两个网址」，还是「看到红灯时想在同一页处理它」？**
> - 前者：一个互链壳页（或书签）零成本治好，不动任何信任边界。
> - 后者：才值得真合并，但它直接撞上你在 spec §10 亲手否决过的「观察面/执行面」权限线（见簇 5）。

AUQ4me 这个名字暗示的如果是「AI 主动向我提问/汇报的个人基础设施」，那真正要设计的是**事件模型和通知路由**（见簇 5 的 #29），合并两个看板只是表象——现在动手可能是在错误的抽象层施工。

---

## 簇 1【P0】信任边界互斥：两个「无认证」语义相反，合并必须选边

**发现**（合并自 merge-shape#0 / product#27 / security#6 / migration#21）：
- task-monitor 的安全模型 = 「tailnet 即信任边界」：`do_GET` 无任何认证分支，**`do_POST /hb` 全程零鉴权**（server.py:70-142），spec §2 把「不上公网」写进非目标。它的安全性 100% 押在「永不上公网」这个前提上，**而合并恰好是击穿该前提的动作**。
- kanban 的「无认证」只限静态壳：index.html 公网可达但绝不嵌 token（server.py:325-332 注释），数据端点全部 Bearer。
- 最自然的合并做法——套用 kanban 现成的 CF→Caddy 反代模板把 8900 挂进统一域名——会**顺手把零认证的 POST /hb 暴露到公网**：任何人 `POST /hb/<name>?status=ok` 即可无条件刷新 last_ok_ts（server.py:110-119），红灯永久变绿、TG 告警被压掉。**监控被毒化 = 假安心，整个系统的存在理由（检测沉默失败）被沉默击穿**。CF-only 闸只挡直连源站，不是认证。
- 事后给 /hb 补认证不是改一个文件：协议无 token 槽位，客户端有两套实现（hb-wrap 纯 POSIX curl + mac guard_common.sh 的 guard_heartbeat），分发在三台机 15 个注入点，且 fail-open 铁律下要先回答「token 失效时心跳静默丢弃算不算违反 fail-open」。

**泄露面**（security#7）：心跳看板一旦公网可见，HTML 里内嵌全部 15 个任务名（cc98-signin / nexushd-signin / webvpn-keeper / zju-watchdog / x-monitor——直接暴露浙大 webvpn、私有 PT 站、监控资产）、三机拓扑、精确 cron 时间窗、以及 **last_error（三台机任意失败任务 stderr 尾 200 字节，常含绝对路径/内网 URL/可能的凭证碎片）**。

**token 升格**（product#30）：kanban 单 token 无 role/scope，手机 localStorage 和 BWG worker 共用同一凭证。今天泄露损失上限是一个任务板；合并后最省事的做法是把监控也挂同一 token 下——手机里那串 64hex 静默升格为「三台机全部任务状态 + 执行引擎触发」的万能钥匙。

**同源 XSS 责任转嫁**（security#8）：两系统今天分属不同 origin，token 结构性不可达。合并同源后，task-monitor 那个从没按「持有凭证页面」标准审过的渲染路径（渲染三台机不可信 stderr，防线只有 _esc），任何未来 XSS = kanban token 失窃。注：/b/* PiliPlus 页已经和 kanban 同 vhost——这个决策其实已经欠了一次。

> **Q1：统一入口放在哪一侧——手机在公网上需要看到任务心跳吗？**
> - 需要 → last_error/cron 时刻表按什么认证强度暴露？要不要脱敏？监控读面挂什么 auth（复用 kanban token = 接受 token 升格；独立 auth = 双 token 体系）？
> - 不需要 → 监控半边永远 tailnet-only，「统一看板」对手机端只剩 kanban 一半，还叫合并吗？
>
> **Q2：POST /hb 写面是否宣布为 AUQ4me 之外的永久冻结独立 listener（tailnet 直连 :8900，Caddy 永不反代）？** 若纳入统一入口 = 一次跨三台机、两套客户端实现的加认证协同改造。
>
> **Q3：人和 agent 要不要分凭证、监控只读和执行触发要不要分权限（哪怕只是两个 token）？** 还是明确接受单 token 就是整个 infra 的 root，并按此重估存放与轮换？

---

## 簇 2【P0】实体与存储：「task」同形异义，没人定义过 AUQ4me 的核心实体

**发现**（data#10 / merge-shape#2 / data#12 / data#13）：
- monitor 的 task = **永续健康态资源**：name 主键、UPDATE-in-place、永不完成、绿黄红摆动、零 DELETE 路径、时间戳强制服务端 epoch int 秒。
- kanban 的 card = **一次性流动事件**：日期+uuid 命名、六态状态机有生有死、body 只增不减、ISO 本地时区字符串。
- 连 research-kanban-worker 自己都同时是两种实体：在 monitor 里是一个永续 task，它跑的每张卡又是会死的 card——合并看板上这两者是什么关系，没人回答过。
- 统一数据模型任一方向都是灾难：心跳写频塞进 md 模型 = 高频全文件重写；卡片进 SQLite = 丢掉可 grep、可直接编辑、可裸 rsync 的文档性，同时瓦解 worker 文件沙箱的设计前提。
- 隐性债提前引爆：kanban 读路径是 CLAIM_LOCK 下**双重全量 glob+parse**（sweep + list 各扫一遍，O(全部历史卡)），现在只有 2 张卡所以无感；「AI infra 长期积累」的愿景会直接继承这颗雷。
- 两边都只增不删（kanban 无 do_DELETE、无归档入口；monitor 连未注册垃圾行都只封顶不清理），spec 明文拒绝历史，而「infra」定位暗示要历史/审计——**保留策略无人认领**。

> **Q4：AUQ4me 的核心实体是什么——永续任务和一次性卡片是同一实体的两种状态（统一 schema，要调和主键/状态机/时间戳三重反差），还是两种类型各留各的模型、合并只发生在 UI 层？**
>
> **Q5：done 卡的归档/删除策略是什么？心跳要不要从「当前态快照」升级为事件流？**（当前规模无紧迫性，但合并设计时必须拍板记录）

---

## 簇 3【P0】监控器的自我参照悖论：合并会让「防静默失效的系统」自己静默失效

这是整个分析里最具体、最容易真实发生的一组。

**watchdog 空库静默模式**（availability#16，P0）：watchdog 与 server 共享 SQLite 仅靠「代码同目录」的隐式约定（`DB_PATH = HB_DB 或 hbcommon.py 所在目录/task-monitor.db`）。合并搬目录后，只要两进程解析出不同路径，watchdog 的 `ensure_schema`（CREATE TABLE IF NOT EXISTS）会**在错误路径上凭空造出合法空库，SELECT 返回 0 行，打印 checked=0 后 exit 0，timer 全绿，告警层从此完全静默**——没有「tasks 表为空」哨兵，mac 兜底只 ping server 的 /health，watchdog 自身无人看守。

**mac 兜底 shim 退化**（availability#15，P1）：mac 每日兜底 ping 硬编码 `http://100.87.113.14:8900/health` 在 plist 的 shell 字符串里（连环境变量口都没有）。合并改端口/路由时，图省事留一个 :8900 兼容 shim 的话，兜底从「探活监控中心」退化为「探活 shim」——真中心死了它照样 200。且 /health 本就只证 HTTP 线程活着（无条件 200，不查 DB 可写、不查 watchdog 是否在 tick），合并后一个 200 要为两套系统背书。

**变更节奏错配 / 故障域焊死**（availability#17/#24/#31，P1）：kanban 是全系统迭代最快的组件（3 天 5 个前端版本，几乎天天在改），task-monitor 是告警器官，正确性前提是**比所有被监控对象更稳**。后端合一意味着：给 AUQ4me 加任何功能 = 重启并冒险改动心跳中心；kanban 一个 bug 拖挂 server 时，红灯页和心跳接收一起消失，唯一兜底是 mac 每日一次 ping，盲窗最长 24h。且 watchdog 的告警词汇表只有「{name}@{machine} 已 X 没成功」，**表达不了「中心自身停摆」**——一次 kanban 侧 bug 换来的是 15 个任务按阈值梯度（15min~26h）陆续变红的 trickle 误归因告警（跨 tick 过线，STORM_THRESHOLD=3 聚合实际拦不住）。

**现存假绿（今天就成立，合并放大）**（data#14 / migration#24，P1）：worker poll 失败（kanban server 挂了）仅 log 后 `return`（exit 0），hb-wrap 据 rc=0 照打 **ok 心跳**；且 kanban server 进程本身不在 tasks.json 监控清单（只监控了 worker timer 节奏）。「worker 🟢」与「看板已死/卡片全卡死」可以同时为真。合并成统一健康视图后，这从跨系统监控缺口升级为 AUQ4me 对自己一半躯体的结构性自欺。

**unit 语义冲突**（availability#18，P1）：两套互斥的「等 tailscale」策略——monitor 是崩溃即重启（StartLimitIntervalSec=0 + Restart=always），kanban 是进程内 600s 忍耐（unit 无 Restart=，默认 no）。合并 unit 选错组合（进程内 raise + Restart=no）= 某次重启 tailscale 慢过 10 分钟，整个 AUQ4me 开机死透不复活。单进程双端口方案还有独有的「半绑定窗口」：第一个 socket bind 后第二个还在重试、serve_forever 未跑，三台机 hb-wrap 的 curl connect 成功后干等 --max-time 8——把 review 里专门修掉的「给任务尾注满 8s 延迟」原样复活。

**资源预算首次被迫回答**（availability#20，P2）：1GB 机（实测 avail 392M），worker 里的 claude cap 850M，两个 server unit 今天都没有任何 Memory*/OOMScoreAdjust。合并写新 unit 的那一刻是第一次必须回答「监控中心的资源预算和 OOM 保护级别」。

> **Q6：watchdog 保持独立进程直读 SQLite 吗？若保持，用什么机制强制两进程共享同一 DB 真源（单一 env 文件；watchdog 发现 tasks 表为空时自告警而非静默 exit 0）？谁监控 watchdog 自己最近一次 tick？**
>
> **Q7：「合并」的进程边界画在哪——你接受「给 AUQ4me 加任何功能 = 重启心跳中心」吗？还是 UI 层合并、心跳采集保持独立最小进程？**
>
> **Q8：:8900（含 mac 兜底 ping 的 URL）承诺原样永久不动吗？若动：禁止 shim，5 处注入文件一次改齐，/health 要不要升级为「DB 可写 + watchdog N 分钟内 tick 过」？**
>
> **Q9：合并 unit 的 Restart/StartLimit/bind-retry 抄哪套？双端口能否先 bind 全部 socket 再统一 activate？要不要给 server 设 MemoryMax + 负值 OOMScoreAdjust、顺手把 worker 850M cap 调低到全局 OOM 打不响的水位？**
>
> **（顺手修，不依赖合并）kanban server 进程补进监控；worker poll 失败改为打 fail 心跳而不是 exit 0。**

---

## 簇 4【P0】割接工程：在「无备份的唯一副本」上做手术，且有 rsync 覆盖活库的前科

**发现**（data#11 / migration#22 / merge-shape#5 / availability#19 / migration#23 / migration#25 / critic C4）：
- cards/ 和 task-monitor.db **全网只有 bwg 一份，谁都没备份**（bwg 备份 r4s，没人备份 bwg；tasks.json 里天天报绿的 backup 任务是 r4s 的——监控给出「已备份」的错觉）。r4s 上可一键恢复的只是 07-03 停用前的陈旧 kanban，不是回滚点。
- 两种存储的备份语义互不通用：task-monitor.db 开 WAL，热 cp/rsync 可能撕裂，必须 sqlite3 `.backup` 或停写；cards/*.md 因 os.replace 原子写可以裸 rsync。若卡片迁进 SQLite，「文件复制=备份」的直觉从此静默作废。
- 部署史有 P0 前科：本地空 DB 差点经 rsync 覆盖 bwg 活心跳库，防线是 README 里四条 --exclude 规则——**与现目录布局强耦合，合并改布局后不再自动对齐**；kanban 侧 cards/ 从来没有部署脚本，第一次纳入 rsync 范围时是零防护。
- HB_CENTER 是**跨三台机的分布式冻结契约**：hb-wrap 三份拷贝（r4s 是 OpenWrt ash）+ mac guard_common.sh 独立实现 + mac 兜底 plist 硬编码整条 URL + 15 个注入点（crontab/LaunchAgents 安装态不在任何 git 仓库）。curl 是不带 -L 的裸 POST，靠 301 过渡不可行。漏改任何一点，fail-open 让心跳静默打向死端口，暴露形式是数小时到 26h 后的一波莫名超时告警。
- 割接窗口没有 runbook：停机 >15min 开始按阈值梯度 trickle 假红；恢复告警无聚合逐条刷屏；热拷 DB 则 alerted/last_ack 状态在新旧库分叉。
- URL 根所有权：kanban 前端全部 fetch 是根相对路径，事实上绑死 origin 根——路径前缀方案下 kanban 永久占根、后来者全是二等租户（还要和 /b/* 排优先级）；新域名方案则要复制整套 CF A 记录+橙云+tls internal+CF IP 段闸，手工维护的静态 IP 清单变成第二份会腐烂的拷贝。
- **Caddy 是被合并的第三个系统**，同一进程还坐着与此无关的生产租户 panel/sub（你日常网络的基础设施）；这台 Caddy 的 systemctl reload 卡死两个月、验证要 --resolve 匹配 SNI，reload 原子性只拦语法非法、拦不住合法但错误的路由——合并调试期反复改配置，手滑一次爆炸半径是订阅断服。

> **Q10：动手前先做什么——给 cards/ + task-monitor.db 建异机备份（db 走 .backup 导出），并把回滚点定义为哪份一致性快照（两个 /opt 目录 + 4 个 unit + Caddyfile）？**
>
> **Q11：割接窗口 watchdog 停掉（接受三机监控盲区）还是不停（接受 2-4 条假红/恢复消息）？窗口上限定多长（15min 是第一条红线）？搬库走「停旧→拷→起新」冷序列吗？**
>
> **Q12：入口形态选路径前缀（kanban 永久占根）还是新域名（CF 闸全套双份维护）？Caddy 改造前要不要先把租户拆成独立 import 片段 + 固化三站点冒烟验证流程？**

---

## 簇 5【P1】权限与事件语义：合并静默重开你亲手否决过的决策

**观察/执行权限线**（product#26 / merge-shape#4）：task-monitor 的只读性目前由物理架构强制（进程里不存在执行代码路径），不需要任何人记得。你在 spec §10 亲手把「看板加重跑按钮」标为「越权到执行层，本期只读」。合并后红灯卡片和「运行 Agent」按钮共处同一界面，这条线从架构保证退化为记忆里的一句话，且「看到红灯点一下重跑」的产品引力被放大到一次点击的视觉距离——而现有执行通道技术上是死路（worker 无 Bash、限 workspace，14 个被监控任务里 10 个在 mac/r4s 上根本不在这台机）。真重跑 = 一套独立立项的跨机执行系统 + 中心获得向另外两台机推送命令的全新能力类别。

**注入链**（security#9）：合并会预置一条从未被威胁建模的管道：外部站点内容 → 任务 stderr 尾 → last_error → 卡片正文 → claude worker prompt（原样拼接）。今天三重弱化（HB_LOG 基本未启用、200B 截断、worker 沙箱），但「红任务一键建卡让 claude 排查」是合并后最顺手的集成，而让排查有用恰恰要求放开日志长度、放宽 worker 工具——管道同时变宽变危险。

**通知语义分裂**（product#29）：任务失败 → TG 即推；卡片失败/重试耗尽 → 只写一行 body 注记，零通知。合并后同一页上有些红色 ping 手机、有些红色永远沉默，没有规则解释区别。如果 AUQ4me 的愿景是「AI 主动向我汇报/提问」，**统一的事件→通道路由模型才是核心资产**，review 列就是天然的「待你回答队列」——它现在是全系统唯一没有通知的部分。

**机器消耗无聚合预算**（critic C2）：现有闸门全是单次粒度（$2/次、30min、3 重试）。今天卡片全是人手建的，人就是限速器；合并后最顺手的集成（红任务自动建卡、告警触发 agent、定时巡检）每一个都把卡片生产交给机器，理论上限 15 任务 × 3 重试 × 30min，烧的是你个人订阅的同一池配额，高峰期挤占你自己的交互会话。

> **Q13：「界面只观察、不触发执行」这条线还算数吗？算——用什么机制（而非记忆）继续强制；不算——接受跨机执行通道是独立立项。**
>
> **Q14：事件分级规则（哪些推 TG / 哪些只亮红灯 / 哪些进「等我来答」队列）——这三行规则你现在能写出来吗？写不出来，说明合并动手太早。**
>
> **Q15：允许机器自动产生 claude 执行吗？若允许，全局预算（每日卡数/美元）在哪一层强制，配额走个人订阅还是拆独立硬上限 key？监控数据进 prompt 前按什么信任等级处理？**

---

## 簇 6【P1】维护者与制度（六路工程视角的结构性盲区，批判者补充）

**日历**（C0）：PTE 考试约 8 月初，距今一个月；这套栈的实证节奏是每个交付都裂变成多轮修复循环（kanban 3 天 5 版每版拖一轮审查；monitor 一次交付 18 条修复）。已确认的盲区清单意味着合并是这套 infra 史上最大单项工程——**被打断不是尾部风险，是基线预期**。半合并态（部分端点已迁、防线未跟上）比「两套各自完整」更差，且可能一停两个月。

**制度不对称**（C1）：monitor 有 git + spec + 5 个测试文件 + implementation-notes；kanban 在 /opt 里零 README、零测试、零部署脚本，「版本控制」是服务器上 5 个 .bak 文件，设计只存在于 memory。合并强制选边：kanban 补课入库（一次从未排期的工程），或告警关键系统的变更流程整体降级为「ssh 改活文件留 .bak」。monitor 的回归测试假设现有模块布局，合并重构后最省事的路径恰恰是静默丢弃它们。

**运行时合同**（C3）：两边都是 Rocky 9 系统 Python 3.9 + stdlib-only，这份合同只活在两个旧文件的惯性里没人写下来。合并是部署史上最大的新代码事件，执行者（大概率是 agent）默认写 3.10+ 语法——溜进一个 match/联合类型就是 import 时 SyntaxError，合并后的单进程整体拒绝启动，心跳中心陪葬（而 watchdog 表达不了「中心自身停摆」）。本地 mac 测试跑新版 Python，语法违约恰好只在 bwg 生产解释器上爆。

> **Q16：排期定在 PTE 之前还是之后？若现在动手，是否硬性约束「每个阶段结束都是可无限期停留的稳定态」，且第一个稳定态是什么（建议：只做备份 + 互链壳页，不动任何端点）？**
>
> **Q17：AUQ4me 的源码真源定在哪个 git 仓库？kanban 半边要不要先入库才有资格被合并？monitor 的测试套件是保留义务还是允许失效？**
>
> **Q18：运行时合同显式钉死「Python 3.9 + stdlib-only」（写进 spec，部署前跑 3.9 的 compileall）还是把升级解释器列为前置步骤？**

---

## 被对抗核实击杀的发现（示范判定标准）

1. ~~「监控平面与业务平面合并 = OOM 一杀两黑没人提醒」~~ —— 告警平面（watchdog+timer）本就是独立进程直读 DB，server 死了 watchdog 照样发 TG；已按事实修正并入簇 3。
2. ~~「合并改名撞上心跳 name 主键、没有文档写过」~~ —— spec §7 有专门一行「任务改名/新增」，README「停用/增改任务」一节把关键点都写了，属已知已覆盖。

## 附：本次分析没有覆盖的

- 没有评估任何具体合并方案的优劣（那是 spark/写方案阶段的事，且依赖上面 Q0-Q18 的答案）。
- 没有做 kanban index.html 全量 1243 行的逐行安全复审（只审了 token 流与渲染边界相关路径）。
- mac / r4s 两台机的实机状态未重新核对（引用的是 spec 注入点表和记忆，非本次实测）。
