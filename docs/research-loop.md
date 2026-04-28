# chat-daily research loop

这个流程把 `autoresearch` 的实验纪律迁移到 `chat-daily-tg`：

1. 固定一批微信导出 fixture。
2. 每次只改一个变量。
3. 跑一次完整生成或离线解析。
4. 记录结果到 `output/research/results.tsv`。
5. 只保留通过验证的改动，失败样本转成回归测试。

默认流程不会发送 Telegram，也不会要求多厂商 API key。

## 阶段 1：离线 smoke test

这一阶段只验证解析、Telegram 格式化、截断识别和 TSV 记录，不调用真实模型。

```bash
cd /Users/Apple/Projects/chat-daily-tg
source .venv/bin/activate
python scripts/research_loop.py \
  --experiment-id offline-sample-html \
  --sample-output tests/fixtures/summary_output_sample.txt \
  --parse-mode HTML
```

结果写入：

```text
output/research/results.tsv
```

这个目录在 `.gitignore` 的 `/output/` 规则下，适合长期积累本地实验结果。

## 阶段 2：用当前配置跑真实 baseline

这一阶段只需要当前 `~/chat-daily/config.yaml` 里配置的模型 API key。

```bash
cd /Users/Apple/Projects/chat-daily-tg
source .venv/bin/activate
python scripts/research_loop.py \
  --experiment-id baseline-current-model \
  --fixture tests/fixtures/wx_export_raw_sample.md \
  --parse-mode HTML \
  --notes "current config baseline"
```

默认仍然是 Telegram dry-run：只检查格式和分片，不真实发送。

## 阶段 3：单变量实验

每轮只改一个变量，例如：

- `--max-tokens 8000`
- `--max-tokens 16000`
- `--parse-mode HTML`
- `--parse-mode MarkdownV2`
- `--model <另一个模型名>`

示例：

```bash
python scripts/research_loop.py \
  --experiment-id max-tokens-8000-html \
  --fixture tests/fixtures/wx_export_raw_sample.md \
  --max-tokens 8000 \
  --parse-mode HTML
```

## 阶段 4：多厂商模型对比

只有这一阶段才需要不同厂商的 API key。建议不要一次性全部接入，先按顺序验证：

1. 当前配置模型 baseline。
2. 同一厂商不同模型。
3. 另一个厂商模型。

每个厂商用独立的环境变量和配置文件，避免把真实 key 写进仓库。

## 可记录指标

`results.tsv` 会记录：

- 是否成功解析三段 fence。
- 是否疑似截断。
- 摘要长度。
- Telegram 分片数。
- MarkdownV2 渲染是否成功。
- LLM usage 里的 prompt/completion/total tokens。
- 耗时。
- 错误信息。
- 人工备注。

## 什么时候真实发送 Telegram

只有测试 chat 才建议加 `--send-telegram`：

```bash
python scripts/research_loop.py \
  --experiment-id telegram-test-chat-html \
  --fixture tests/fixtures/wx_export_raw_sample.md \
  --parse-mode HTML \
  --send-telegram
```

不要把长期实验直接发到正式群。
