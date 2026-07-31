# Contributing

## Scope first

Keep changes small and state which pipeline and invariant they affect. This repository contains machine-specific deployment history; a public contribution must not silently rewrite launchd, r4s cron, route tables, databases or operator data.

Before coding, read:

- `README.md` for setup and user-facing boundaries;
- `docs/ARCHITECTURE.md` for data flow;
- `docs/testing.md` for evidence levels;
- `SECURITY.md` for secret and report handling.

## Development setup

```bash
uv sync --extra dev --locked
uv run chat-daily --help
```

Do not edit `uv.lock` unless dependency metadata actually changes. Explain every new dependency, why the standard library or an existing dependency is insufficient, and its runtime/build/CI impact.

## Required checks

```bash
uv run pytest -q -m "not e2e" --ignore=tests/e2e
uv run pytest -q -m e2e tests/e2e
uv build --no-sources

uvx --from ruff==0.11.13 ruff format --check \
  src/chat_daily_tg/raw_seen.py tests/test_raw_seen.py tests/e2e \
  scripts/benchmark_seen_store.py
uvx --from ruff==0.11.13 ruff check \
  src/chat_daily_tg/raw_seen.py tests/test_raw_seen.py tests/e2e \
  scripts/benchmark_seen_store.py
uvx --from mypy==1.16.1 mypy \
  src/chat_daily_tg/cli.py src/chat_daily_tg/features src/chat_daily_tg/raw_seen.py
```

The Ruff scope is incremental. Do not combine a functional fix with a repository-wide formatter pass.

## Delivery invariants

A contribution must preserve these contracts:

- enhancements fail open toward body delivery;
- seen is write-after-send, including every album message ID;
- `--no-push` does not count as delivery and must not write `.run-complete`;
- LLM output is parsed, normalized, validated and given a code fallback;
- Bilibili API/CDN requests remain direct with `trust_env=False`;
- Telegram/Gemini may use the intended HTTP(S) proxy, while the entry point clears `ALL_PROXY/all_proxy`;
- ledger consumers do not become unauthorized writers;
- secrets and real data stay outside the repository.

Add or update tests that fail before the fix and pass after it. Mock tests must be labeled as unit or hermetic E2E, never as production verification.

## Pull request content

Describe:

1. the concrete problem and affected function/file;
2. why the chosen patch is the smallest safe boundary;
3. tests and benchmarks actually run;
4. tests not run and why;
5. runtime, lockfile, configuration and deployment impact;
6. rollback steps for stateful changes.

Use placeholders or synthetic fixtures only. Redact tokens, chat IDs that identify private groups, source text, route tables, local paths and database contents.
