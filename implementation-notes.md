# Implementation Notes

## Design Decisions

- Evidence indexing is narrowed at chunk-selection time: keep high-risk messages and adjacent informative context, instead of embedding every exported chat line.
- Fact-risk reporting is generated from verifier JSON after the verified summary is parsed, so it reflects the final verifier decision rather than the draft.

## Deviations

## Tradeoffs

- Adjacent short context is kept when it sits next to a high-risk message, because terse chat replies like “这个能直接读x” can be crucial evidence even though they are short.

## Open Questions

---

## 2026-06-02 — Push audit fixes + image output

### Design Decisions
- **Image output** is an *add-on*, not a replacement: `run_daily` sends the PNG card first (when `telegram.send_image` is true, default **false**), then ALWAYS sends the full HTML text — so the proven text path is the fallback and no information is lost. Gated behind a config flag so behavior is unchanged until opted in.
- **Card renderer toolchain**: system `Google Chrome --headless=new --screenshot` + plain f-string HTML, chosen over Jinja2/Playwright because the `.venv` has neither package — the Chrome-subprocess path adds zero Python deps and is byte-deterministic/offline, which suits the unattended launchd run. `card_renderer.parse_concise_to_card` reuses the fixed `### emoji` heading schema from `prompts.py` as its parser contract.
- **SEC-1 log redaction** done at the *formatter* level (`_RedactingFormatter`) so it scrubs both the message and the exception traceback (where the httpx error embeds the bot-token URL). Regex has no `\b` anchors because the token appears as `.../bot<digits>:.../` — a leading word boundary never matches there.
- **SEC-2**: secrets are now `.env`-only. Stripped `DEEPSEEK_API_KEY`/`TG_BOT_TOKEN`/`TG_CHAT_ID` from both the installed launchd plist and the tracked template (values were verified identical to `.env` first), keeping only `PATH`. `run_daily` already calls `load_env_file(DATA_DIR/.env)`, verified to supply all secrets in a clean env. Reloaded the agent.

### Deviations
- `_send_one` no longer formats; formatting moved up into `send()` so chunking happens on the FINAL payload (CHUNK-1). Split limit lowered 4096→3900 for margin. Existing tests still pass because net formatted output is unchanged.

### Open Questions
- Caption is a plain-text teaser (first overview line). If a richer caption is wanted later, it must avoid truncating an open `<b>` tag — keep it plain text.
- `deploy.sh` still has a label mismatch (`com.chat-daily.tg` vs real `com.chat-daily-tg.agent`) so it currently no-ops on the plist; now that the template carries no secrets it is safe to fix the label, but that was out of scope for this pass.
