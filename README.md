# wx-daily-tg

Daily WeChat group summary → Telegram bot, powered by local CLIProxyAPI (uses your Claude Code subscription, no paid API key).

## What it does

Every morning at 08:00 (macOS launchd):
1. Exports yesterday's messages from configured WeChat groups via `wx-cli`
2. Summarizes via Claude (through local CLIProxyAPI)
3. Pushes concise summary to your Telegram bot
4. Archives detailed summary + raw exports under `~/wx-daily/archive/YYYY/MM/DD/`
5. Extracts long-term opportunities (invite codes, bank products) → `permanent.jsonl`
6. Extracts short-term opportunities (arbitrage, bugs) → `hot-leads/` with 14-day rolloff
7. Scans for death signals ("关门了" / "封了") to auto-mark dead items

## Setup (one-time)

1. Install wx-cli and run `sudo wx init` (see wx-cli docs — requires codesign WeChat.app)
2. Install & configure CLIProxyAPI, start it on `127.0.0.1:8317`
3. Create Telegram bot via @BotFather, obtain token + your chat_id
4. `cd /Users/Apple/projects/wx-daily-tg && python3 -m venv venv && source venv/bin/activate && pip install -e ".[dev]"`
5. Configure `~/wx-daily/config.yaml` (list target groups, matching `wx sessions` output)
6. Export these env vars in `~/.zshenv`:
   ```
   export CLIPROXY_API_KEY="..."
   export TG_BOT_TOKEN="..."
   export TG_CHAT_ID="..."
   ```
7. Run `./scripts/install-launchd.sh` to install the daily schedule

## Run manually

```bash
source venv/bin/activate
python run_daily.py              # yesterday (default)
python run_daily.py --date 2026-04-17
```

## Test

```bash
pytest -v
```

All 55 unit tests should pass.

## Upgrade model

Edit `~/wx-daily/config.yaml` `llm.model` field. No code change. Validate available models:

```bash
curl -s http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer $CLIPROXY_API_KEY" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m['id'] for m in d['data']))"
```

Current: `claude-sonnet-4-6`. Alternate for Chinese: `kimi-k2.5`.

## Troubleshooting

- **No TG message arrives:** `tail -f ~/wx-daily/logs/*.log` — check for Telegram API errors
- **Export empty:** WeChat must be running + logged in when launchd fires. If Mac was asleep, launchd fires on wake.
- **Model not available:** `/v1/models` should list it. Check CLIProxyAPI `config.yaml`.
- **LLM timeout:** default is 300s. Bump `llm.timeout` in config.yaml if Claude runs long on big prompts.
- **launchd not firing:** `launchctl list | grep wx-daily-tg` — should show 1 entry.

## Data layout

```
~/wx-daily/
├── config.yaml
├── permanent.jsonl          # long-term opportunities (invite codes, products)
├── permanent.md             # human-readable view (auto-generated)
├── hot-leads/
│   ├── latest.md            # 14-day rolling active window (auto-generated)
│   └── 2026/04/17.md        # that day's new hot leads (only if non-empty)
├── archive/
│   └── 2026/04/17/
│       ├── <group>.md       # raw wx-cli exports
│       └── summary.md       # LLM-generated detailed summary
└── logs/
    └── 2026-04-17.log
```

## Architecture

Single-machine Python 3.11+ pipeline. All components local. See `docs/specs/2026-04-18-design.md` for design decisions and `docs/plans/2026-04-18-wx-daily-tg.md` for implementation.

## License

Personal use.
