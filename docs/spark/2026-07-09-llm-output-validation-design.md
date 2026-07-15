# Spec: LLM 输出信任边界校验

日期：2026-07-09
状态：已确认，待实现
范围：仅 chat-daily-tg 仓库
来源：对比 LYiHub/labs-ArchiveAssistant 后借鉴其"prompt 闭集声明 + 解析侧校验 + 非法值回落默认"成对纪律；本仓库 `docs/notes/implementation-notes.md`（2026-06-11 条目）已记录 value_score 不稳与 should_include_in_daily 死字段问题。

## 问题

prompt 里声明了闭集/值域，但解析侧不校验，LLM 越界输出原样入库：

1. `hot_leads.category`（prompts.py 声明 `arbitrage|bug|personal_trick|gray_zone`）与 `permanent.category`（`invite_code|bank_product|activity|misc`）、`permanent.type`（`permanent|product|activity`）：代码只做空值兜底（`or "misc"` 等，run_daily.py:105/113/131），枚举外值直接入库。
2. vision `value_score`：qwen 偶返 >1（PDF 分享判过 2.5），`_normalize_score`（vision.py:79-89）用 ">1 则 /10" 猜测式归一，其余越界值无处理。
3. vision `should_include_in_daily`：prompt 索取、解析保留，但筛选只看 `value_score >= 0.8`（vision.py:132），字段是死代码。

## 决策记录

- `should_include_in_daily`：**启用为 AND 门**（否决"从 prompt 删除"和"保持现状"）。方向与 2026-07-02 用户把阈值 0.65→0.8 一致：只更严不更松。
- 枚举外值处理：回落默认 + log 原始值（否决拒绝入库——机会数据宁可归错类不可丢）。
- 越界分数处理：按 0.0 处理（保守方向，必然被阈值排除；否决 clamp 到 1.0——会把 0-10 制下的低分图捧成满分）。

## 设计

### 枚举校验（run_daily.py 机会持久化路径）

新增小函数（不做配置项）：

```python
def coerce_enum(value, allowed: set[str], default: str, field_name: str) -> str
```

- 空值 → default（保持现行为）；合法值 → 原样；枚举外 → default + `log.warning("field %s got out-of-enum %r, coerced to %s", ...)`。
- 三处接入：`hot_leads.category` → 默认 `arbitrage`；`permanent.category` → 默认 `misc`；`permanent.type` → 默认 `permanent`（各自沿用现有空值默认，语义不变）。

### vision 数值校验（vision.py `_normalize_score`）

- 保留 `(1,10] → /10` 归一（该失败模式下 /10 语义正确：2.5 意为 2.5/10=0.25 低分）。
- 归一后仍在 `[0,1]` 外（负数、>10、非数值）→ 返回 0.0 + warning。

### AND 门（vision.py 日报入选判定）

- 入选条件：`value_score >= min_include_score(0.8)` **且** `should_include_in_daily != False`。
- 字段缺失或非布尔 → 视为 `True`（退化为纯分数门 = 现行为，模型漏字段时不突变）；显式 `False` → 高分也排除。
- 这是本 spec 唯一行为变更，记入 implementation notes。

### 测试

- 枚举：三字段各覆盖 枚举外→默认+warning不崩 / 空值→原默认 / 合法值原样。
- 数值：`2.5→0.25`、`15→0.0`、`-0.2→0.0`、非数值→`0.0`。
- AND 门：高分+False→排除；高分+缺失→入选；低分+True→排除。
- 存量 tests/test_vision.py 等全绿。

### 范围外

- 不改 prompt 文本（枚举声明已存在）。
- 不加告警推送（log 即可——这是防御纪律，不是事故）。
- 不动 verifier / death_signals 的既有校验逻辑。
