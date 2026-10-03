# 内容迭代完成度核对

核对对象：2026-09-29 内容迭代设计。代码符号由 AST 验证存在；完成状态结合已有运行产物，符号存在不等于验收通过。

| 要求 | 当前证据 | 剩余工作 |
| --- | --- | --- |
| P0-01 · 30 条完整真实原文，四类来源与标签 | 未完成；`src/chat_daily_tg/content_replay.py::freeze` | v3四类来源共30条已具备；人工标签0、上下文及ASR待复审 |
| P0-02 · 事件分组、开发/留出隔离与冻结 | 实现已验证；样本待复审；`src/chat_daily_tg/content_replay.py::freeze` | 当前全部开发集，未取得人工事件分组 |
| P0-03 · 双规则模型回放与失败回执 | 实现已验证；实测待完成；`src/chat_daily_tg/content_replay.py::model_replay` | 三十条精选样本未跑当前/候选双规则 |
| P0-04 · 反馈 owner/目标/幂等与持久化 offset | 本地已验证；`src/chat_daily_tg/content_feedback.py::accept` | 远端完整信封只读查询已接通；本机启用待明确确认 |
| P0-05 · 展开返回唯一关联完整原文 | 部分完成；`src/chat_daily_tg/content_feedback.py::resolve` | 新增公开文本与20份媒体原文目录已接入；21个媒体入口匹配，旧文本账本待补 |
| P0-06 · 反馈失败重启恢复、未知回复人工复核 | 本地已验证；`src/chat_daily_tg/content_feedback.py::review_response` | 尚无真实回复/失败恢复试用 |
| P0-07 · 反馈试用成功率、指令数、展开数 | 未验收；`src/chat_daily_tg/content_feedback.py::summary` | 没有真实试用回执 |
| P0-08 · 健康抓取与内容时间、失败/空结果 | 部分完成；`src/chat_daily_tg/content_health.py::collect_fetch_health` | 主要入口已插桩，本机14条真实回执已核对；远端运行未验证 |
| P0-09 · 健康通知合并与恢复 | 本地已验证；`src/chat_daily_tg/content_operations.py::notify_health` | 真实 controller/Telegram 接收未验收 |
| P0-10 · 未知投递统一清单与人工重试 | 部分完成；`src/chat_daily_tg/content_operations.py::daily_review` | 成长和回复接通；其他投递入口的全量导入与重试未齐 |
| P1-01 · 价值 profile 与程序排序 | 已真实验证流程；`src/chat_daily_tg/value_profiles.py::preview` | 12条公开内容、14次调用通过；质量尚未人工评测 |
| P1-02 · 临界/分歧二次评审、原排序回退 | 已真实验证流程；`src/chat_daily_tg/value_profiles.py::preview` | 真实执行2次二次评审 |
| P1-03 · 日报预览接入 | 未完成；`src/chat_daily_tg/value_profiles.py::preview` | 日报唯一链接/显式多源映射预览已接入；实际日报仍23条待补映射 |
| P1-04 · 两组10条盲评及漏选/耗时 | 待人工；`src/chat_daily_tg/value_profiles.py::blind_result` | 已打开12条候选盲评页面，尚未收到结果 |
| P1-05 · rubric候选、评测、复审与切换回滚 | 本地已验证；真实待完成；`src/chat_daily_tg/rubric_candidates.py::activate` | 20条成长样本没有人工标签；在线规则未切换 |
| P1-06 · rubric 周报候选差异与完整分歧 | 本地已验证；`src/chat_daily_tg/growth_weekly.py::build_weekly_report` | 需要真实候选回放报告 |
| P1-07 · 关注3–5事件与来源关联复审 | 部分完成；`src/chat_daily_tg/event_files.py::suggest` | 已选5个，8条材料均为候选；逐条人工归属未确认 |
| P1-08 · 新增事实、冲突、更正、撤销与快照 | 部分完成；`src/chat_daily_tg/event_files.py::rebuild` | 实现原文引用和版本；真实错误合并/拆分/漏进展评测缺失 |
| P1-09 · 来源独立性与质量周报 | 本地已验证；真实待完成；`src/chat_daily_tg/source_quality.py::weekly_quality` | 独立出处与有用率没有人工标签 |
| P1-10 · 移除Jev预算，保留通用L2工作量上限 | 代码已核对；`src/chat_daily_tg/jev_judge.py::judge` | 开始时已经移除；未动线上抑制开关 |
| P1-11 · Jev原始回执、缓存、失效与降级 | 本地已验证；真实待完成；`src/chat_daily_tg/call_receipts.py::report` | 真实Jev 1次网络调用与1次复用已验收；持续运行待验证 |
| P2-01 · 沿用检索发布门禁、降级与回滚 | 本地已验证；`src/chat_daily_tg/knowledge_cli.py::cmd_task` | 未启用线上检索发布 |
| P2-02 · 已读/已投递与7天检索，最多3结果 | 部分完成；`src/chat_daily_tg/knowledge_tasks.py::task_results` | 范围已前移至exact/FTS/dense截断前；实际generation向量缺失，词法诊断可用 |
| P2-03 · 20条真实个人找回问题实测 | 待人工；`src/chat_daily_tg/knowledge_tasks.py::task_results` | 尚未取得20条真实问题与期望原文 |
| DEL-01 · 目标机器与真实Telegram回执 | 未验收；`src/chat_daily_tg/content_iteration_cli.py::main` | 本机任务直接读取工作树，已有采集回执；真实反馈/通知回执和远端运行未齐 |

目标尚未完成。优先补齐原文样本、日报预览接入及非成长入口的未知投递导入，同时等待人工标签与检索问题。
