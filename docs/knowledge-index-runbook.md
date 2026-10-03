# KnowledgeIndex operator runbook

The KnowledgeIndex is a rebuildable, side-band derivative. None of its commands
send Telegram messages, advance seen/marker state, consume updates, or append to
the delivery ledgers. Keep the long-term retrieval, daily-evidence enhancement,
and L2 enforcement releases separate.

## Read-only source scan

```bash
uv run chat-daily-knowledge scan
```

The scan validates every selected fact source and reports source-specific
cursors. Archive ingestion is allowlisted to original Telegram/WeChat Markdown
plus optional `summary.md`/`concise.md`; other generated reports are excluded.

## Build and verify a generation

Use an explicit safe generation name so interrupted builds can be resumed:

```bash
uv run chat-daily-knowledge build --generation qwen-vl-4096-YYYYMMDD-HHMM
uv run chat-daily-knowledge build --generation qwen-vl-4096-YYYYMMDD-HHMM --resume
uv run chat-daily-knowledge verify --generation qwen-vl-4096-YYYYMMDD-HHMM --full
```

Build uses the longer offline runtime timeout. Online `query` and
`canary-query` use an eight-second hard deadline and degrade to lexical/RRF when
the dense or reranker path is unavailable. ChatDaily embedding, rerank, online
query, and offline backfill clients share a bounded cross-process priority
queue keyed by runtime endpoint; a waiting online query enters before queued
offline batches.

`query` is a production surface, not a generation inspector: it requires an
open release record bound to the verified `CURRENT`/`PREVIOUS` pair. A YAML
feature flag or `--enable-retrieval` never substitutes for that release record.
Use `diagnostic-query --generation ...` for a side-band generation; diagnostic
queries are explicitly non-production and never append release telemetry.

## L2 delivered-window backfill

Always inspect a copy first. Dry-run is the default and opens SQLite read-only:

```bash
uv run python scripts/backfill_delivered_embeddings.py \
  --db /path/to/delivered_index.copy.db \
  --config ~/chat-daily/config.yaml
```

Only after the copy has passed apply and idempotency checks should the same
bounded operation target the derived live database:

```bash
uv run python scripts/backfill_delivered_embeddings.py \
  --db ~/chat-daily/state/delivered_index.db \
  --config ~/chat-daily/config.yaml \
  --max-rows 256 --max-seconds 300 --apply
```

Coverage below 99.5%, an uncalibrated Qwen generation, or a generation mismatch
keeps L2 in report/fail-open mode. Old vectors with missing provenance are not
treated as Qwen merely because they are 4096-dimensional.

## Seven-day shadow and read-only canary

Record runtime health at least hourly (for example from a dedicated scheduler):

```bash
uv run chat-daily-knowledge shadow-probe qwen-vl-4096-YYYYMMDD-HHMM
uv run chat-daily-knowledge shadow-audit-sources \
  qwen-vl-4096-YYYYMMDD-HHMM \
  --journal ~/chat-daily/index/shadow/events.jsonl
uv run chat-daily-knowledge shadow-status qwen-vl-4096-YYYYMMDD-HHMM
```

`shadow-audit-sources` reads the configured default trusted fact sources and
appends validated daily `source_freshness`/source-link observations; it does not
send, advance markers, or refresh the generation. Manual derived observations can
not be appended with `shadow-record`; the CLI rejects all release-evidence
kinds unless they come from their authoritative producers. A release is not
shadow-ready until it has seven
distinct observation dates spanning at least six full days, at least 168
hourly health samples, availability at least 99.5%, seven consecutive
successful incremental refresh receipts on seven distinct consecutive UTC
dates, seven source-link audits, and no bad source links.
The legacy `shadow-record` entry point rejects every release-evidence kind; it
cannot supply or backfill health, query, source-freshness, incremental-refresh,
or source-link evidence.
Query observations are deduplicated by `request_hash`; health readiness counts
distinct UTC hours, so repeated events cannot manufacture 168 hours or dilute
an unavailable sample. A health sample is available only when the runtime also
attests the candidate manifest's exact embedding and reranker revisions;
`ready=true` from another or unversioned runtime does not count. Every candidate
observation records both
`reranker_attempted` and `reranker_error`; the guard counts only real reranker
attempts in its 10-minute numerator and denominator and rejects ambiguous legacy
rows instead of treating them as successful requests.

`source_freshness success=true` means the cursor comparison completed. Its
`noop=true` form proves only that all authoritative source cursors still equal
the candidate's recorded snapshot. A valid changed snapshot is
`success=true, noop=false`; malformed, missing, or ambiguous cursor evidence is
`success=false`. No `source_freshness` row, including a no-op, counts toward the
incremental gate.

When trusted sources have changed, run a real refresh with a new output ID:

```bash
uv run chat-daily-knowledge incremental-refresh \
  qwen-vl-4096-SHADOW-CANDIDATE \
  --output-generation qwen-vl-4096-INCREMENTAL-YYYYMMDD-HHMMSS \
  --journal ~/chat-daily/index/shadow/events.jsonl
```

The command first full-verifies the immutable shadow candidate against the
configured runtime identity. A no-op appends freshness only and creates no
output generation or countable receipt. A real change builds a separate
generation from the current facts, embeds every output chunk again, full-
verifies vectors and catalog, rechecks source cursors and links, and only then
appends an authoritative `incremental_refresh_receipt`. It never changes
`CURRENT`, `PREVIOUS`, or `SHADOW_CANDIDATE`. Vectors are never copied across
generations: the specification permits reuse only when both chunk hash and
generation ID are identical. The receipt binds the candidate, sealed output,
and baseline/snapshot/output cursor hashes; duplicate same-day receipts cannot
inflate the seven-day gate and any valid failed receipt pessimistically fails
that UTC date.

The repository includes a guarded hourly collector and a LaunchAgent template.
It is opt-in and must not be installed until a real candidate exists:

```bash
printf '%s\n' qwen-vl-4096-YYYYMMDD-HHMM > ~/chat-daily/index/SHADOW_CANDIDATE
CHAT_DAILY_INSTALL_KNOWLEDGE_SHADOW=1 bash scripts/install-launchd.sh
```

The LaunchAgent fires every 15 minutes. Independent due gates run health at most
once per real hour and trusted-source audit at most once per real 24 hours;
only a successful producer advances its own gate. A failed producer remains due
for the next 15-minute tick and does not cause the other producer to rerun or
count extra observations. Installing the template reloads launchd labels, so
first confirm there is no in-flight ChatDaily job. This scheduler remains
opt-in: repository tests and synthetic journals do not satisfy the required
real 168-hour observation.

The guarded wrapper's real refresh lane is independently opt-in and disabled by
default. Enabling it still requires a regular non-symlink
`SHADOW_CANDIDATE` pointer and does not install or reload launchd:

```bash
CHAT_DAILY_KNOWLEDGE_INCREMENTAL_REFRESH_ENABLED=1 \
  bash scripts/run_knowledge_guarded.sh
```

Its due gate runs at most once per 24 hours. A successful no-op advances the due
gate but does not create an incremental receipt; only a successful fresh rebuild
can advance the release readiness count.

After activation, a stable request key can drive the 10% read-only cohort. The
new `CURRENT` is the candidate and `PREVIOUS` is the default baseline; a
candidate-open failure automatically serves that baseline:

```bash
uv run chat-daily-knowledge canary-query "query text" \
  --enable-retrieval \
  --request-id stable-request-key \
  --candidate qwen-vl-4096-YYYYMMDD-HHMM \
  --percent 10 --record
```

## Evaluation, activation, and rollback

Activation requires both a passing frozen evaluation of at least 200 annotated
queries and a complete seven-day shadow journal:

Each gold-set JSONL row must carry `query`, non-empty
`relevant_content_ids`, non-empty `expected_source_refs`, `source_kind`, and
`kind`. Release composition is fail-closed: at least 40 exact identity cases,
60 semantic/paraphrase cases, 40 cross-source same-event cases, and 30
long-form/article/transcript cases are required. The remaining cases can cover
other text strata; add at least 30 `image`/`ocr` cases when the image phase is
enabled. Always supply the same-Mac, same-query-set shadow baseline so the 20%
E2E regression gate is evaluated rather than silently skipped.

When a label is specific to one conversation bundle or transcript fragment,
also provide non-empty `expected_member_ids` and/or `expected_locators`. Citation
accuracy then binds the returned chunk to those exact identities instead of
crediting a different chunk from the same content item. Evaluation preserves
unrounded per-query E2E durations; any individual query above 8,000 ms blocks
release even when the aggregate p95 remains below eight seconds.

`exact_recall` is stricter than overall Recall@50: every relevant identity in
an exact/url/content-ID/BVID/YouTube-ID/model case must be returned through the
`exact` retrieval channel. An identity found only by FTS or dense retrieval is
credited to Recall@50 but not to exact recall, so semantic retrieval cannot mask
a stale or broken `exact_terms` index.

```bash
uv run chat-daily-knowledge evaluate gold.jsonl \
  --generation qwen-vl-4096-YYYYMMDD-HHMM \
  --output evaluation.json \
  --baseline-generation qwen-vl-4096-BASELINE

uv run chat-daily-knowledge activate qwen-vl-4096-YYYYMMDD-HHMM \
  --evaluation evaluation.json \
  --shadow-journal ~/chat-daily/index/shadow/events.jsonl
```

`CURRENT`/`PREVIOUS` changes are locked and journaled. Automatic rollback uses
only a complete `chatdaily-knowledge-guard-metrics.v1` snapshot produced from
authoritative inputs. Produce it atomically before evaluating it:

```bash
uv run chat-daily-knowledge guard-snapshot \
  --current-evaluation evaluation-current.json \
  --baseline-evaluation evaluation-previous.json \
  --shadow-journal ~/chat-daily/index/shadow/events.jsonl \
  --output ~/chat-daily/index/guard/metrics.json

uv run chat-daily-knowledge guard ~/chat-daily/index/guard/metrics.json
uv run chat-daily-knowledge guard ~/chat-daily/index/guard/metrics.json --execute
```

The producer binds the snapshot to the live `CURRENT` and `PREVIOUS` pointers,
the embedding and reranker model IDs/revisions, dimension, the current manifest
SHA-256, runtime config SHA-256, both evaluation file hashes, and the append-only
shadow-journal hash. The current evaluation must be fresh (24 hours by default),
contain at least 200 queries, and use the same frozen gold-set hash as the
`PREVIOUS` baseline. The shadow journal supplies rather than estimates:

- the most recent 10-minute reranker error numerator, denominator, and rate;
- three ordered, contiguous 10-minute p95 windows with a non-zero sample count;
- seven-day source-link audit and bad-link counts.

The guard recomposes the entire canonical snapshot from those files immediately
before any mutation. It rejects a missing field, extra or wrong-typed value,
NaN/Infinity, a snapshot older than 20 minutes, a changed input hash or manifest,
and any cross-generation evaluation. Rejection exits non-zero without changing
`CURRENT`, `PREVIOUS`, `RETRIEVAL_DISABLED`, or the wrapper's applied marker.

For a manually maintained guard snapshot, configure both evaluation paths or
neither:

```bash
export CHAT_DAILY_KNOWLEDGE_GUARD_CURRENT_EVALUATION=/absolute/evaluation-current.json
export CHAT_DAILY_KNOWLEDGE_GUARD_BASELINE_EVALUATION=/absolute/evaluation-previous.json
bash scripts/run_knowledge_guarded.sh
```

When configured, the wrapper runs `guard-snapshot` before `guard`; a failed
snapshot producer or failed guard is never marked as applied. Health/source
producer failures still return a non-zero wrapper status but do not bypass an
otherwise due guard: successful guard execution retains its own fail-closed
marker semantics. With no snapshot producer configured, an existing snapshot is
still strictly revalidated by the real guard and cannot be made healthy by
omitting fields.

The wrapper also has an opt-in paired-evaluation producer. It is disabled in
the launchd template and must stay disabled until a reviewed, frozen gold JSONL
with at least 200 real annotations exists. To exercise the producer explicitly:

```bash
export CHAT_DAILY_KNOWLEDGE_PAIRED_EVALUATION_ENABLED=1
export CHAT_DAILY_KNOWLEDGE_FROZEN_GOLD=/absolute/reviewed-gold.jsonl

# Optional; defaults are shown. Producer outputs are restricted to guard/.
export CHAT_DAILY_KNOWLEDGE_GUARD_CURRENT_EVALUATION="$HOME/chat-daily/index/guard/evaluation-current.json"
export CHAT_DAILY_KNOWLEDGE_GUARD_BASELINE_EVALUATION="$HOME/chat-daily/index/guard/evaluation-baseline.json"

bash scripts/run_knowledge_guarded.sh
```

On a due run, the producer reads regular non-symlink `CURRENT` and `PREVIOUS`
pointers, measures baseline then current in one process over one frozen query
order, and stages both sides of that paired receipt. It publishes them only if
the evaluation passes and the pointers and gold-file SHA-256 remain unchanged.
The subsequent `guard-snapshot` binds the published file hashes back to the
live pointers, manifests, runtime configuration, and shadow journal. The due
gate refreshes a successful pair every 12 hours by default; override both
`CHAT_DAILY_KNOWLEDGE_EVALUATION_DUE_MIN_S` and
`CHAT_DAILY_KNOWLEDGE_EVALUATION_DUE_MAX_S` only with an explicit operating
policy.

Missing or small gold, missing/identical pointers, unsafe paths, evaluation or
extraction failure, and input changes all return non-zero. Such a run does not
replace prior receipts, does not create a guard snapshot, and does not apply an
older metrics file as if it were fresh. Failed runs remain due for the next
wrapper retry. Enabling the template, installing it, or reloading launchd is a
separate deployment action; these source changes do none of those actions.

Execution is incident-journaled per failed generation under
`guard/incidents/<failed-generation>.json` and is idempotent: repeated or updated
failing metrics cannot swap back to the failed generation. Historical incidents
are retained when a later release is evaluated. The global `RETRIEVAL_DISABLED`
latch makes `query` and `canary-query` fail closed even if the YAML release flag
remains true. A later controlled promotion may clear it only when the latch is
cryptographically and uniquely bound to the completed incident for that
release's baseline, and both artifacts predate the pending release. Ambiguous,
changed, unsafe, or unrelated evidence leaves the latch in place for manual
review. That genuinely new `CURRENT`/`PREVIOUS` pair may then create one new
incident and roll back once. Rollback never deletes a generation and never
writes to facts, ledgers, seen state, markers, or business databases.
