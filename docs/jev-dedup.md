# Jev 话题去重

Jev 可以作为频道 L2 话题去重的主裁判接入现有频道进程。向量检索找到达到相似度门槛的候选后，裁判把新卡片和最多三条已送达候选发送到 TypeSafe System One。Jev 返回“是否同一事件”和“新增信息量”；请求失败、响应格式错误或返回 uncertain 时，程序回退到原有 SameEventJudge，两者都失败则沿用 L2 的相似度降级规则。

接入代码在 [jev_client.py](../src/chat_daily_tg/jev_client.py)、[jev_judge.py](../src/chat_daily_tg/jev_judge.py) 和 [application.py](../src/chat_daily_tg/application.py)。TypeSafe 接口是 [POST /v1/systemone](https://docs.typesafe.ai/api)，模型固定为 jev-latest，布尔问题使用 noul，密钥变量是 TYPESAFE_API_KEY。客户端只接受官方 endpoint，并把请求超时、重试次数和响应字段限制在代码定义的范围内。

将以下片段合并到 ~/chat-daily/config.yaml；保留原有数据源、embedding 和备用裁判配置。字段定义见 [config.py](../src/chat_daily_tg/config.py) 的 DedupTopic、JevModel 和 JevJudgePolicy。

    sources:
      telegram:
        dedup:
          topic:
            enabled: true
            mode: report
            enforce_enabled: false
            judge_provider: jev
            qwen_runtime_on_demand: true
            qwen_runtime_start_timeout_seconds: 180
            jev_same_event_threshold: 0.5
    models:
      jev:
        enabled: true
        endpoint: https://api.typesafe.ai/v1/systemone
        model: jev-latest
        api_key_env: TYPESAFE_API_KEY
        timeout: 3
        retry_max_attempts: 2
        zero_data_retention: false

密钥保存在 ~/chat-daily/.env，文件权限为 600。TYPESAFE_BASE_URL 只用于组成默认 endpoint；程序仍会校验最终地址。zero_data_retention 当前必须为 false，因为 TypeSafe 原生 HTTP 接口没有文档化的 ZDR 请求参数。

示例按 mode: report 运行，Jev 的判断进入 L2 决策；该模式记录原本会采取的动作，最终仍投递。启用抑制前需要完成向量覆盖、reranker、校准回执和线上观察门槛。投递、seen、dedup_journal 和 .run-complete 的状态语义不因 Jev 失败而改变。主裁判结果写入 ~/chat-daily/state/jev-dedup-judge.jsonl，只保存候选 ID、输入哈希、模型结果和延迟，不保存卡片正文或密钥。

Jev 主裁判和 shadow 均不预留 HTTP 次数，也不维护 Jev 专属单轮或日累计额度。旧配置中的 `jev_judge_max_attempts_per_run`、`jev_judge_daily_cap`、`jev_shadow_max_calls_per_run`、`jev_shadow_daily_cap` 按额外字段忽略，可在下次编辑配置时删除；历史 `.budget.json` 文件保持原样，程序不再读取或写入。

通用 L2 单轮判定上限仍由 [config.py](../src/chat_daily_tg/config.py) 的 `DedupTopic.max_judge_calls_per_run` 控制，适用于 Jev 和原 LLM。达到上限后，[TopicDedupGate.assess](../src/chat_daily_tg/topic_dedup.py) 沿用相似度降级规则，不再调用裁判。单次 HTTP 超时与有界重试由 [JevClient](../src/chat_daily_tg/jev_client.py) 控制；shadow 继续按 `JevPolicy.jev_shadow_sample_rate` 抽样。

频道任务不需要新增 Jev 进程。launchd 的 com.chat-daily-tg.channels 下一次启动会加载 ~/chat-daily/config.yaml，构造 Jev 主裁判；Jev 不可用时日志会记录失败类型并继续使用原裁判。qwen_runtime_on_demand 会在频道任务需要向量检索时按需启动已有的本地 Qwen 服务，最多等待 180 秒完成冷启动；任务结束后在白天释放，凌晨资源窗口内不会重复停止服务。

下面的命令只检查裁判能否按配置构造，不请求模型：

    cd /Users/Apple/Projects/chat-daily-tg
    env -u ALL_PROXY -u all_proxy uv run python - <<'PY'
    from pathlib import Path
    from chat_daily_tg.config import load_config
    from chat_daily_tg.env import load_env_file
    from chat_daily_tg.jev_judge import build_jev_judge
    load_env_file(Path.home() / "chat-daily/.env")
    cfg = load_config(Path.home() / "chat-daily/config.yaml")
    print(build_jev_judge(cfg) is not None)
    PY

离线样本回放仍使用 [evaluate_jev_dedup.py](../scripts/evaluate_jev_dedup.py)。只读真实链路检查使用 [probe_jev_dedup.py](../scripts/probe_jev_dedup.py)；它复制送达索引到实验目录，按需启动 Qwen，调用真实 Jev，并且不发送 Telegram。冻结样本、人工标签和历史报告保存在 ~/chat-daily/experiments/jev-dedup/，它们用于校准和复核，不会覆盖当前频道运行状态。

Node 服务端 smoke 示例见 [examples/jev-sdk](../examples/jev-sdk/README.md)。npm test 只验证模拟 HTTP；真实调用需要 ~/chat-daily/.env 中的 TypeSafe key。

真实链路诊断会使用送达索引副本，结果写入新的空目录：

    uv run python scripts/probe_jev_dedup.py --out ~/chat-daily/experiments/jev-dedup/YYYYMMDD-primary-probe

诊断把一条历史卡片重放到当前候选集，可验证启动、检索和真实 API 调用。去重准确率需要独立标签回放。实测结果见 [2026-09-22 记录](notes/jev-dedup-2026-09-22.md)。


跨 producer caption 镜像与带图帖子规则见 [去重运行指南](dedup-policy.md)。

## 调用回执与响应复用

主裁判和 shadow 构造的 `JevClient` 将回执写入运行状态目录下的 `jev-calls/calls.sqlite3`。
[CallReceipts](../src/chat_daily_tg/call_receipts.py) 管理事务写入与文件权限；
[JevClient.evaluate](../src/chat_daily_tg/jev_client.py) 管理校验、失效、绕过与故障降级。
复用身份包含服务、模型、输入、问题定义和策略版本。模型别名使用短有效期，
`bypass_cache=True` 强制取得新响应。复用结果记录 `origin=reused`、原回执引用与零网络尝试，
不重复计算 usage。存储不可用时继续调用模型，失败响应不进入成功缓存。

该库同时保存已校验的结构化响应和 HTTP 响应正文原始字节，不保存请求头。原始正文可能包含私有内容，
仅存于权限 0600 的运行数据库；进入回放材料前需脱敏。回执保存原始响应引用、字节数与 SHA-256，
原始响应归档失败时记录失败类型并继续现有处理。

`content_iteration_cli calls-report` 汇总已持久化的网络尝试、复用、失败类型和耗时。
缺少终态的 started 记录按年龄列入待复核，不更改原记录、不触发重试。存储失败导致缺失的回执不计入统计。
