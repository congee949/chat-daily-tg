# Testing and verification boundaries

This project deliberately separates three kinds of evidence. Do not call one layer by another layer's name.

## 1. Unit tests

Unit tests exercise functions or small components with test doubles and temporary files. They are fast and explain local contracts such as parsing, retry behavior, album handling, marker semantics and LLM-output fallback.

```bash
uv sync --extra dev --locked
uv run pytest -q -m "not e2e" --ignore=tests/e2e
```

Expected assertion: every non-E2E test passes on supported Python versions. A unit pass does not prove the installed CLI, real configuration loading or an HTTP request boundary works end to end.

## 2. Hermetic E2E

The hermetic E2E suite starts at the public CLI and uses the real application code through the external HTTP boundary. It does not use real credentials or make network calls.

```bash
uv run pytest -q -m e2e tests/e2e
```

Current channel-forwarder coverage:

1. invokes `chat_daily_tg.cli.main(["channels", "run"])`;
2. runs process preparation, feature dispatch and legacy application orchestration;
3. loads a real temporary YAML config;
4. reads a real temporary SQLite `messages` table;
5. builds and archives a real channel card;
6. sends through the real `TelegramSender` into `httpx.MockTransport`;
7. verifies the routed `chat_id` and `message_thread_id`;
8. verifies seen is absent while the HTTP request is in flight and is written only after a successful response;
9. verifies a Telegram HTTP 400 with HTML parse mode degrades to plain text, still without an early seen write;
10. verifies `ALL_PROXY` and `all_proxy` are cleared before the HTTP client path.

This is an E2E of the local process and its HTTP contract. It is not a real Telegram, LLM, WeChat, Bilibili, YouTube, proxy, launchd, cron or production-data test.

## 3. Real integration or production verification

Real verification uses operator-owned credentials and infrastructure. It must be manual or run in a separately controlled environment; it must not be added to public GitHub Actions secrets by default.

Recommended order:

1. use test data and `--no-push` to validate local export and archive generation;
2. use a dedicated test Bot and test chat for one real Telegram delivery;
3. inspect the message, local logs, seen state and delivery marker;
4. only then enable launchd or r4s cron;
5. observe at least one scheduled execution and one controlled failure/retry.

Report real verification with the date, environment class, command, redacted outcome and observed artifacts. Never paste tokens, source messages, route tables or database contents.

## CI gates

The proposed CI has independent jobs so a failure identifies the boundary:

- quality: incremental Ruff format/lint and MyPy checks;
- unit: Python 3.11 and 3.13 unit suites;
- e2e: Python 3.11 hermetic CLI-to-HTTP suite;
- build: source distribution, wheel, clean-environment wheel install and CLI smoke test.

Ruff and MyPy are invoked with pinned `uvx --from ...` versions. They are intentionally not added to project runtime or dev dependencies, so this patch does not change `uv.lock`. The quality scope is incremental rather than pretending the legacy tree has already been reformatted or fully typed.

## Performance evidence

`SeenStore.max_msg_id()` is called once per configured raw channel during incremental forwarding. The previous implementation scanned every historical seen key for every call. The indexed implementation computes the same result with a dictionary lookup.

Run the reproducible microbenchmark:

```bash
uv run python scripts/benchmark_seen_store.py \
  --channels 50 \
  --messages-per-channel 2000 \
  --queries 2000
```

The script asserts result equivalence and reports both wall-clock time and work count:

- scan baseline: `number_of_seen_keys × queries` key visits;
- indexed implementation: `queries` dictionary lookups.

Do not set a CI pass/fail threshold on wall-clock speedup. Shared runners are noisy; semantic equivalence and operation-count reduction are the stable evidence.
