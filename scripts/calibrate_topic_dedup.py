#!/usr/bin/env python3
"""Offline calibration for the L2 topic-dedup layer — run BEFORE any enforcement.

Measures the REAL same-event collision rate on the delivered surface (the
Telegram notification forum group -1004424841223, where ALL producers' pushes
land) and produces a human-review markdown report that sets candidate_min_sim /
strong_sim and resolves the design's stated unknowns:

  - how are sendRichMessage posts / macrumors captions stored (corpus samples)
  - does sender_name separate producers for free (producer x sender cross-tab)
  - macrumors <-> 科技圈 and X <-> X collision counts
  - go/no-go: if same-event collisions are ~0/week, L2 stays dark

Stages (each individually skippable, each prints progress):
  1. precondition probe   forum group must be visible to the tg-cli session
  2. tg sync              (--no-sync to skip; failure degrades to existing rows)
  3. corpus report        producer/sender attribution over the --days window
  4. embedding            (--no-embed to skip; spend requires --yes/confirm)
  5. all-pairs cosine     rolling 48h windows, cross-producer/cross-sender only
  6. judge sample         (--judge opt-in; spend requires --yes/confirm)
  7. report file          ~/chat-daily/state/topic-dedup-calibration-<date>.md

Read-only by construction except the report file and the tg-cli sync: no
SeenStore writes, no sends, and NO DeliveredIndex writes — messages.db is read
directly so the production index stays untouched by calibration.

Exit codes: 0 ok, 2 precondition failed (group unreachable / empty corpus).

KNOWN CURRENT STATE (2026-07-16): the forum group is NOT in the tg-cli
account's dialogs, so stage 1 WILL fail today with exact fix-it guidance.
Once the account (@Congee123) is added to the group and synced, the same
script runs end-to-end without edits.

Usage:
  .venv/bin/python scripts/calibrate_topic_dedup.py                 # full run
  .venv/bin/python scripts/calibrate_topic_dedup.py --no-sync --no-embed
  .venv/bin/python scripts/calibrate_topic_dedup.py --judge --yes   # incl. LLM
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat_daily_tg.telegram_exporter import (  # noqa: E402
    LOCAL_TZ,
    canonical_chat_ids,
    parse_timestamp,
    sync_chat,
)

# Reuse the production functions — calibration must measure exactly what the
# gate will later compute, so nothing here is re-implemented. _MIN_GATE_CHARS
# is private but load-bearing: texts the gate would never assess must not
# skew the calibration corpus either.
from chat_daily_tg.topic_dedup import (  # noqa: E402
    DEFAULT_EXCLUDE_PRODUCERS,
    IndexedMsg,
    SameEventJudge,
    _MIN_GATE_CHARS as MIN_GATE_CHARS,
    cosine,
    guess_producer,
    normalize_for_embedding,
)

FORUM_CHAT_ID = "-1004424841223"
GROUP_INTERNAL_ID = "4424841223"  # 2-segment deep link base: t.me/c/<this>/<msg_id>
TG_ACCOUNT = "@Congee123 (id 8113034240)"
DEFAULT_DB = Path.home() / "Library/Application Support/tg-cli/messages.db"
JUDGE_MODEL = "gpt-5.6-terra"
PAIR_WINDOW = timedelta(hours=48)
DETAIL_MIN_SIM = 0.75
HIST_LO, HIST_HI, HIST_STEP = 0.50, 1.00, 0.05
BANDS: tuple[tuple[float, float], ...] = ((0.75, 0.80), (0.80, 0.87), (0.87, 0.93), (0.93, 1.01))
JUDGE_SAMPLE_PER_BAND = 8  # ~30 total across 4 bands

EXIT_OK = 0
EXIT_PRECONDITION = 2

log = logging.getLogger("calibrate_topic_dedup")


# --------------------------------------------------------------------------- #
# report accumulator: everything printed is also captured for the md file

class Report:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def line(self, text: str = "") -> None:
        print(text, flush=True)
        self.parts.append(text)

    def block(self, text: str) -> None:
        """Print verbatim; land in the md inside a code fence."""
        print(text, flush=True)
        self.parts.append(f"```\n{text}\n```")

    def md_only(self, text: str) -> None:
        self.parts.append(text)

    def render(self) -> str:
        return "\n".join(self.parts) + "\n"


@dataclass
class Msg:
    msg_id: int
    ts: datetime
    ts_str: str
    sender: str
    text: str
    producer: str
    norm: str
    vector: list[float] | None = None

    def head(self, n: int = 120) -> str:
        return " ".join(self.text.split())[:n]

    def local_ts(self) -> str:
        return self.ts.astimezone(LOCAL_TZ).strftime("%m-%d %H:%M")


@dataclass
class Pair:
    sim: float
    earlier: Msg
    later: Msg

    def producers(self) -> tuple[str, str]:
        return (self.earlier.producer, self.later.producer)


# --------------------------------------------------------------------------- #
# stage 1: precondition probe

PRECONDITION_GUIDANCE = f"""\
PRECONDITION FAILED: the notification forum group {FORUM_CHAT_ID} is not
visible to the tg-cli user session (zero rows in messages.db AND `tg info`
could not resolve the chat).

The tg-cli account is {TG_ACCOUNT}. To fix:
  1. Add that account to the notification forum group ({FORUM_CHAT_ID}).
  2. Run:  tg sync -n 3000 -- {FORUM_CHAT_ID}
  3. Re-run this script.

Nothing was written. Exit code 2 = precondition failed.
"""


def count_forum_rows(db_path: Path) -> int:
    """Rows for the forum group in messages.db; 0 when db/table is absent."""
    ids = sorted(canonical_chat_ids(FORUM_CHAT_ID))
    placeholders = ",".join("?" for _ in ids)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                f"SELECT COUNT(*) FROM messages WHERE chat_id IN ({placeholders})", ids
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"  (messages.db not readable at {db_path}: {e} — treating as zero rows)")
        return 0


def tg_info_ok() -> bool:
    """Best-effort `tg info -- <chat>`. tg exits 0 even when it cannot find
    the chat (verified 2026-07-16: rc=0 + 'Could not find chat: …' on stdout),
    so the output is sniffed as well as the return code."""
    try:
        proc = subprocess.run(
            ["tg", "info", "--", FORUM_CHAT_ID],
            capture_output=True, text=True, timeout=60,
        )
        out = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == 0 and "could not find chat" not in out.lower():
            return True
        detail = next((ln for ln in out.strip().splitlines()
                       if "could not find chat" in ln.lower()), None)
        detail = detail or (out.strip().splitlines() or ["(no output)"])[-1]
        print(f"  tg info failed (rc={proc.returncode}): {detail.strip()}")
        return False
    except Exception as e:
        print(f"  tg info not available ({type(e).__name__}: {e})")
        return False


def stage_probe(db_path: Path) -> bool:
    print(f"== stage 1/7: precondition probe ({FORUM_CHAT_ID} in tg-cli session) ==")
    n = count_forum_rows(db_path)
    print(f"  messages.db rows for forum group: {n}")
    if n > 0:
        print("  probe OK: group already present in messages.db")
        return True
    print("  zero rows — checking `tg info` as a fallback (best-effort)…")
    if tg_info_ok():
        print("  probe OK: group resolvable via tg — the sync stage will populate rows")
        return True
    print()
    print(PRECONDITION_GUIDANCE)
    return False


# --------------------------------------------------------------------------- #
# stage 2: sync

def stage_sync(args: argparse.Namespace) -> None:
    print("\n== stage 2/7: tg sync ==")
    if args.no_sync:
        print("  skipped (--no-sync)")
        return
    print(f"  tg sync -n {args.sync_limit} -- {FORUM_CHAT_ID} …")
    try:
        sync_chat(FORUM_CHAT_ID, limit=args.sync_limit)
        print("  sync OK")
    except Exception as e:
        print(f"  WARNING: sync failed ({type(e).__name__}: {e}) — continuing with existing rows")


# --------------------------------------------------------------------------- #
# stage 3: corpus report

def load_corpus(db_path: Path, days: int) -> tuple[list[Msg], int]:
    """All non-empty forum rows inside the window; also returns the count of
    empty/media-only rows skipped. Direct read — never via DeliveredIndex."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ids = sorted(canonical_chat_ids(FORUM_CHAT_ID))
    placeholders = ",".join("?" for _ in ids)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT msg_id, sender_name, content, timestamp FROM messages "
            f"WHERE chat_id IN ({placeholders}) ORDER BY msg_id ASC",
            ids,
        ).fetchall()
    finally:
        con.close()

    msgs: list[Msg] = []
    empty = 0
    for r in rows:
        try:
            ts = parse_timestamp(str(r["timestamp"]))
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        text = (r["content"] or "").strip()
        if not text:
            empty += 1
            continue
        msgs.append(Msg(
            msg_id=int(r["msg_id"]),
            ts=ts,
            ts_str=str(r["timestamp"]),
            sender=str(r["sender_name"] or ""),
            text=text,
            producer=guess_producer(text),
            norm=normalize_for_embedding(text),
        ))
    return msgs, empty


def stage_corpus(rep: Report, msgs: list[Msg], empty: int, days: int) -> None:
    rep.line()
    rep.line(f"## Corpus ({days}-day window)")
    rep.line()
    rep.line(f"- rows with text: {len(msgs)}  (plus {empty} empty/media-only rows skipped)")
    span = f"{msgs[0].local_ts()} → {msgs[-1].local_ts()}" if msgs else "(empty)"
    rep.line(f"- time span (Asia/Shanghai): {span}")

    by_producer = Counter(m.producer for m in msgs)
    rep.line()
    rep.line("### Per-producer counts (topic_dedup.guess_producer)")
    rep.line()
    for name, n in by_producer.most_common():
        rep.line(f"- {name}: {n}")
    other_pct = 100.0 * by_producer.get("other", 0) / max(len(msgs), 1)
    flag = "  ⚠ HIGH — guess_producer needs hardening before enforce mode" if other_pct > 20 else ""
    rep.line(f"- guessed 'other': {other_pct:.1f}%{flag}")

    by_sender = Counter(m.sender for m in msgs)
    rep.line()
    rep.line("### Per-sender counts (does sender separate producers for free?)")
    rep.line()
    sender_producers: dict[str, Counter] = defaultdict(Counter)
    for m in msgs:
        sender_producers[m.sender][m.producer] += 1
    for sender, n in by_sender.most_common(15):
        mix = ", ".join(f"{p}={c}" for p, c in sender_producers[sender].most_common())
        rep.line(f"- {sender or '(none)'}: {n}  [{mix}]")
    multi = [s for s, c in sender_producers.items() if len(c) > 1]
    rep.line(f"- senders carrying >1 guessed producer: {len(multi)} "
             f"({'sender does NOT separate producers for free' if multi else 'sender DOES separate producers'})")

    rep.line()
    rep.line("### Samples (resolves 'how are sendRichMessage posts / macrumors captions stored')")
    for target in ("x_monitor", "macrumors", "chatdaily_raw", "other"):
        picked = [m for m in msgs if m.producer == target][:2]
        rep.line()
        rep.line(f"#### {target} ({by_producer.get(target, 0)} rows)")
        if not picked:
            rep.line("- (no rows guessed as this producer in the window)")
        for m in picked:
            rep.block(f"msg {m.msg_id} · {m.local_ts()} · sender={m.sender or '(none)'}\n{m.head(160)}")


# --------------------------------------------------------------------------- #
# stage 4: embedding

def confirm_spend(what: str, args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print(f"  {what}: not confirmed (non-interactive without --yes) — skipping")
        return False
    answer = input(f"  {what} — proceed? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def build_embedder():
    """Construct GeminiEmbedder exactly the way run_daily does (run_daily.py
    embedding stage): repo loaders for ~/chat-daily/.env + config.yaml, then
    cfg.models.embedding drives every constructor argument."""
    from chat_daily_tg.config import load_config
    from chat_daily_tg.env import load_env_file
    from chat_daily_tg.evidence_index import GeminiEmbedder
    from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR
    import os

    load_env_file(DATA_DIR / ".env")
    cfg = load_config(CONFIG_PATH)
    embedding_model = cfg.models.embedding if cfg.models else None
    if embedding_model is None:
        raise RuntimeError("config has no models.embedding block")
    if not embedding_model.enabled:
        print("  note: models.embedding.enabled is false in config — calibration proceeds anyway")
    api_key = os.environ[embedding_model.api_key_env]
    return cfg, GeminiEmbedder(
        endpoint=embedding_model.endpoint,
        model=embedding_model.model,
        api_key=api_key,
        timeout=embedding_model.timeout,
        output_dimensionality=embedding_model.dimension,
    )


def stage_embed(rep: Report, msgs: list[Msg], args: argparse.Namespace):
    """Returns (cfg, embeddable_msgs) — cfg is reused by the judge stage."""
    rep.line()
    rep.line("## Embedding")
    rep.line()
    excluded = frozenset() if args.include_excluded_producers else DEFAULT_EXCLUDE_PRODUCERS
    eligible = [m for m in msgs if len(m.norm) >= MIN_GATE_CHARS and m.producer not in excluded]
    short = sum(1 for m in msgs if len(m.norm) < MIN_GATE_CHARS)
    rep.line(f"- eligible texts: {len(eligible)} "
             f"(skipped {short} with norm < {MIN_GATE_CHARS} chars; "
             f"excluded producers: {sorted(excluded) or 'none'})")

    if args.no_embed:
        rep.line("- skipped (--no-embed)")
        return None, []
    if not eligible:
        rep.line("- nothing to embed")
        return None, []

    total_chars = sum(len(m.norm) for m in eligible)
    batches = math.ceil(len(eligible) / 100)
    # CJK-heavy text runs ~1 token/char — treat chars as an upper-bound token
    # estimate. Actual spend depends on the GOOGLE_API_KEY tier/pricing.
    rep.line(f"- spend estimate: {len(eligible)} texts, {total_chars} chars "
             f"(≈{total_chars} tokens upper bound), {batches} batchEmbedContents call(s), "
             f"~{max(0, batches - 1) * 16}s inter-batch delay")
    if not confirm_spend(f"embed {len(eligible)} texts via Gemini", args):
        rep.line("- skipped (spend not confirmed)")
        return None, []

    try:
        cfg, embedder = build_embedder()
    except Exception as e:
        rep.line(f"- FAILED to construct embedder ({type(e).__name__}: {e}) — pairs stage will be skipped")
        return None, []

    print(f"  embedding {len(eligible)} texts "
          f"(embedder batches internally at 100/call with backoff)…")
    try:
        vectors = embedder.embed_documents([m.norm for m in eligible])
        if len(vectors) != len(eligible):
            raise ValueError(f"embedder returned {len(vectors)} vectors for {len(eligible)} texts")
        for m, v in zip(eligible, vectors):
            m.vector = [float(x) for x in v]
        rep.line(f"- embedded: {len(eligible)} texts, dimension {len(vectors[0]) if vectors else 0}")
    except Exception as e:
        rep.line(f"- embedding FAILED ({type(e).__name__}: {e}) — pairs stage will be skipped")
        return cfg, []
    return cfg, eligible


# --------------------------------------------------------------------------- #
# stage 5: all-pairs within rolling 48h windows

def stage_pairs(rep: Report, embedded: list[Msg], args: argparse.Namespace) -> list[Pair]:
    rep.line()
    rep.line("## Pairwise similarity (rolling 48h windows)")
    rep.line()
    if len(embedded) < 2:
        rep.line("- fewer than 2 embedded texts — nothing to compare")
        return []

    embedded = sorted(embedded, key=lambda m: m.ts)
    n_buckets = round((HIST_HI - HIST_LO) / HIST_STEP)
    hist = [0] * n_buckets
    below = 0
    total_pairs = 0
    details: list[Pair] = []

    for i, a in enumerate(embedded):
        for j in range(i + 1, len(embedded)):
            b = embedded[j]
            if b.ts - a.ts >= PAIR_WINDOW:
                break  # sorted by ts — every later j is farther away
            if not args.include_same_producer:
                # same-producer same-sender pairs are L1's territory
                if a.producer == b.producer and a.sender == b.sender:
                    continue
            total_pairs += 1
            sim = cosine(a.vector, b.vector)
            if sim < HIST_LO:
                below += 1
            else:
                hist[min(int((sim - HIST_LO) / HIST_STEP), n_buckets - 1)] += 1
            if sim >= DETAIL_MIN_SIM:
                details.append(Pair(sim=sim, earlier=a, later=b))

    rep.line(f"- pairs compared: {total_pairs} "
             f"(<48h apart, {'all producers/senders' if args.include_same_producer else 'different producer OR different sender'})")
    rep.line()
    rep.line("### Similarity histogram")
    rep.line()
    peak = max(max(hist), 1)
    lines = [f"< {HIST_LO:.2f}      {below:5d}"]
    for k in range(n_buckets):
        lo = HIST_LO + k * HIST_STEP
        bar = "#" * round(50 * hist[k] / peak)
        lines.append(f"{lo:.2f}–{lo + HIST_STEP:.2f}  {hist[k]:5d}  {bar}")
    rep.block("\n".join(lines))

    details.sort(key=lambda p: p.sim, reverse=True)
    rep.line()
    rep.line(f"### Pairs ≥ {DETAIL_MIN_SIM} ({len(details)}) — review these by hand")
    if not details:
        rep.line()
        rep.line("- none")
    for p in details:
        rep.block(
            f"cosine={p.sim:.4f}\n"
            f"  A msg {p.earlier.msg_id} · {p.earlier.local_ts()} · {p.earlier.producer} · {p.earlier.sender}\n"
            f"    {p.earlier.head()}\n"
            f"  B msg {p.later.msg_id} · {p.later.local_ts()} · {p.later.producer} · {p.later.sender}\n"
            f"    {p.later.head()}"
        )
    return details


# --------------------------------------------------------------------------- #
# stage 6: judge sample

def band_of(sim: float) -> int | None:
    for idx, (lo, hi) in enumerate(BANDS):
        if lo <= sim < hi:
            return idx
    return None


def build_judge(cfg):
    """The vibekey summary client, constructed the way run_daily's
    _llm_from_block does, with the model overridden to the judge model."""
    from chat_daily_tg.llm_client import LLMClient
    import os

    m = cfg.resolve_model_alias("vibekey")
    llm = LLMClient(
        endpoint=m.endpoint, model=m.model, api_key=os.environ[m.api_key_env],
        max_tokens=m.max_tokens, timeout=m.timeout,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
        extra_body=m.extra_body,
    )
    # SameEventJudge applies overrides to a dataclasses.replace COPY.
    return SameEventJudge(llm, model=JUDGE_MODEL, max_tokens=1000, timeout=120.0)


def stage_judge(rep: Report, cfg, details: list[Pair], args: argparse.Namespace) -> list[dict]:
    rep.line()
    rep.line("## Judge sample")
    rep.line()
    if not args.judge:
        rep.line("- skipped (run with --judge to sample-verify bands with the LLM)")
        return []
    if cfg is None or not details:
        rep.line("- skipped: no embedded pairs available (embedding skipped or failed)")
        return []

    banded: dict[int, list[Pair]] = defaultdict(list)
    for p in details:
        idx = band_of(p.sim)
        if idx is not None:
            banded[idx].append(p)
    rng = random.Random(args.seed)
    sample: list[Pair] = []
    for idx in range(len(BANDS)):
        pool = banded.get(idx, [])
        sample.extend(rng.sample(pool, min(JUDGE_SAMPLE_PER_BAND, len(pool))))
    if not sample:
        rep.line("- skipped: no pairs fall inside the judge bands")
        return []

    rep.line(f"- stratified sample: {len(sample)} pairs "
             f"({', '.join(f'{lo:.2f}-{hi:.2f}: {len(banded.get(i, []))} avail' for i, (lo, hi) in enumerate(BANDS))})")
    rep.line(f"- model: {JUDGE_MODEL} via the vibekey backend, one bounded call per pair")
    if not confirm_spend(f"judge {len(sample)} pairs with {JUDGE_MODEL}", args):
        rep.line("- skipped (spend not confirmed)")
        return []

    try:
        judge = build_judge(cfg)
    except Exception as e:
        rep.line(f"- FAILED to construct judge ({type(e).__name__}: {e})")
        return []

    results: list[dict] = []
    for k, p in enumerate(sample, 1):
        # chronological framing: the LATER message plays the "new card",
        # the earlier one is the already-delivered match.
        match = IndexedMsg(
            msg_id=p.earlier.msg_id, ts=p.earlier.ts_str, producer=p.earlier.producer,
            text=p.earlier.text, norm_text=p.earlier.norm, vector=p.earlier.vector,
        )
        print(f"  judging {k}/{len(sample)} (cosine={p.sim:.3f})…", flush=True)
        v = judge.judge(p.later.text, [match])
        results.append({
            "band": band_of(p.sim), "sim": p.sim, "pair": p,
            "same_event": v.same_event, "new_info": v.new_info,
            "ok": v.ok, "reason": v.reason,
        })

    rep.line()
    rep.line("### Verdict distribution per band")
    rep.line()
    for idx, (lo, hi) in enumerate(BANDS):
        rows = [r for r in results if r["band"] == idx]
        if not rows:
            rep.line(f"- [{lo:.2f}–{hi:.2f}): no pairs judged")
            continue
        ok_rows = [r for r in rows if r["ok"]]
        same = sum(1 for r in ok_rows if r["same_event"])
        info = Counter(r["new_info"] for r in ok_rows if r["same_event"])
        failed = len(rows) - len(ok_rows)
        rep.line(f"- [{lo:.2f}–{hi:.2f}): {len(rows)} judged, same_event={same}/{len(ok_rows) or 1} "
                 f"(new_info: {dict(info) or '{}'}; judge failures: {failed})")

    rep.line()
    rep.line("### Individual verdicts")
    for r in sorted(results, key=lambda r: r["sim"], reverse=True):
        p: Pair = r["pair"]
        rep.block(
            f"cosine={r['sim']:.4f} same_event={r['same_event']} new_info={r['new_info']} ok={r['ok']}\n"
            f"  reason: {r['reason'][:200]}\n"
            f"  A msg {p.earlier.msg_id} [{p.earlier.producer}] {p.earlier.head(100)}\n"
            f"  B msg {p.later.msg_id} [{p.later.producer}] {p.later.head(100)}"
        )
    return results


# --------------------------------------------------------------------------- #
# stage 7: final checklist + report file

def suggest_thresholds(judge_results: list[dict]) -> tuple[str, str]:
    """Lowest band whose judged same-event rate clears 50% → candidate_min_sim;
    90% → strong_sim. Falls back to the shipped defaults when unjudged."""
    if not judge_results:
        return ("0.80 (shipped default — no judge data; review the ≥0.75 pairs above by hand)",
                "0.93 (shipped default — no judge data)")
    candidate = strong = None
    for idx, (lo, _hi) in enumerate(BANDS):
        rows = [r for r in judge_results if r["band"] == idx and r["ok"]]
        if not rows:
            continue
        rate = sum(1 for r in rows if r["same_event"]) / len(rows)
        if candidate is None and rate >= 0.5:
            candidate = lo
        if strong is None and rate >= 0.9:
            strong = lo
    return (
        f"{candidate:.2f} (lowest band with ≥50% judged same-event)" if candidate is not None
        else "0.80 (no band reached 50% same-event — defaults hold, collisions look rare)",
        f"{strong:.2f} (lowest band with ≥90% judged same-event)" if strong is not None
        else "0.93 (no band reached 90% same-event — defaults hold)",
    )


def stage_checklist(rep: Report, msgs: list[Msg], details: list[Pair],
                    judge_results: list[dict], days: int) -> None:
    rep.line()
    rep.line("## Final checklist")
    rep.line()

    cand, strong = suggest_thresholds(judge_results)
    rep.line(f"- [ ] (a) suggested candidate_min_sim: {cand}")
    rep.line(f"- [ ] (a) suggested strong_sim: {strong}")
    rep.line(f"- [ ] (b) verify the 2-segment deep link opens on YOUR clients before "
             f"annotate mode ships: https://t.me/c/{GROUP_INTERNAL_ID}/<msg_id> "
             f"(pick any msg_id from the pair dump above)")

    def cross(p: Pair, x: str, y: str) -> bool:
        return set(p.producers()) == {x, y} if x != y else (
            p.earlier.producer == x and p.later.producer == x
            and p.earlier.sender != p.later.sender)

    mac = [p for p in details if cross(p, "macrumors", "chatdaily_raw")]
    xx = [p for p in details if cross(p, "x_monitor", "x_monitor")]
    rep.line(f"- [ ] (c) macrumors↔科技圈(chatdaily_raw) collisions: "
             f"{len(mac)} at ≥{DETAIL_MIN_SIM}, {sum(1 for p in mac if p.sim >= 0.80)} at ≥0.80")
    rep.line(f"- [ ] (c) X↔X (x_monitor, different sender) collisions: "
             f"{len(xx)} at ≥{DETAIL_MIN_SIM}, {sum(1 for p in xx if p.sim >= 0.80)} at ≥0.80")

    weeks = max(days / 7.0, 0.001)
    ok_judged = [r for r in judge_results if r["ok"]]
    if ok_judged:
        # extrapolate each band's judged same-event rate onto its full pair count
        est = 0.0
        for idx in range(len(BANDS)):
            rows = [r for r in ok_judged if r["band"] == idx]
            n_band = sum(1 for p in details if band_of(p.sim) == idx)
            if rows and n_band:
                est += n_band * (sum(1 for r in rows if r["same_event"]) / len(rows))
        rate = est / weeks
        basis = f"judged extrapolation: ≈{est:.1f} same-event pairs in {days}d"
    else:
        strong_n = sum(1 for p in details if p.sim >= 0.93)
        rate = strong_n / weeks
        basis = f"unjudged proxy: {strong_n} pairs ≥0.93 in {days}d"
    verdict = ("≈0/week → L2 STAYS DARK (do not enforce; re-measure later)"
               if rate < 1.0 else
               f"≈{rate:.1f}/week → collisions are real; proceed with report-mode rollout")
    rep.line(f"- [ ] (d) go/no-go ({basis}): {verdict}")
    rep.line()
    rep.line(f"- corpus note: {len(msgs)} delivered rows analysed; ages shown to the judge "
             f"are historical (calibration runs after the fact), which slightly biases "
             f"the judge toward 'different event' on stale pairs")


def write_report(rep: Report, args: argparse.Namespace) -> Path:
    from chat_daily_tg.paths import STATE_DIR

    out_dir = args.report_dir if args.report_dir else STATE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"topic-dedup-calibration-{datetime.now(LOCAL_TZ):%Y-%m-%d}.md"
    path.write_text(rep.render(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="calibrate_topic_dedup.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"tg-cli messages.db path (default: {DEFAULT_DB})")
    ap.add_argument("--days", type=int, default=14,
                    help="corpus window in days (default: 14)")
    ap.add_argument("--sync-limit", type=int, default=3000,
                    help="tg sync -n limit (default: 3000)")
    ap.add_argument("--no-sync", action="store_true",
                    help="skip stage 2 (tg sync); analyse existing rows only")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip stages 4-6 (embedding, pairs, judge); corpus report only")
    ap.add_argument("--skip-probe", action="store_true",
                    help="skip stage 1 (offline testing; empty corpus still exits 2)")
    ap.add_argument("--judge", action="store_true",
                    help="stage 6: LLM-judge a stratified ~30-pair sample (spend; needs --yes or confirm)")
    ap.add_argument("--yes", action="store_true",
                    help="pre-confirm all spend prompts (embedding + judge)")
    ap.add_argument("--include-same-producer", action="store_true",
                    help="also compare same-producer same-sender pairs (L1's territory, off by default)")
    ap.add_argument("--include-excluded-producers", action="store_true",
                    help=f"embed/pair the gate-excluded producers too ({sorted(DEFAULT_EXCLUDE_PRODUCERS)})")
    ap.add_argument("--seed", type=int, default=42,
                    help="RNG seed for the judge sample (default: 42)")
    ap.add_argument("--report-dir", type=Path, default=None,
                    help="override the report directory (default: ~/chat-daily/state)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Hard rule (CLAUDE.md): a socks ALL_PROXY makes httpx raise at Client()
    # construction — scrub it exactly like run_daily's entry point does.
    from chat_daily_tg.env import scrub_socks_proxy_env
    scrub_socks_proxy_env()

    db_path = args.db.expanduser()

    if args.skip_probe:
        print("== stage 1/7: precondition probe == skipped (--skip-probe)")
    elif not stage_probe(db_path):
        return EXIT_PRECONDITION

    stage_sync(args)

    print("\n== stage 3/7: corpus report ==")
    try:
        msgs, empty = load_corpus(db_path, args.days)
    except sqlite3.Error as e:
        print(f"cannot read {db_path}: {e}")
        print(PRECONDITION_GUIDANCE)
        return EXIT_PRECONDITION
    if not msgs:
        print(f"no forum rows with text inside the {args.days}-day window — nothing to calibrate.")
        print(PRECONDITION_GUIDANCE)
        return EXIT_PRECONDITION

    rep = Report()
    rep.md_only(f"# L2 topic-dedup calibration — {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M %Z}")
    rep.md_only("")
    rep.md_only(f"- forum group: `{FORUM_CHAT_ID}` · db: `{db_path}` · window: {args.days}d")
    rep.md_only(f"- flags: no_sync={args.no_sync} no_embed={args.no_embed} judge={args.judge} "
                f"include_same_producer={args.include_same_producer} "
                f"include_excluded_producers={args.include_excluded_producers} seed={args.seed}")
    stage_corpus(rep, msgs, empty, args.days)

    print("\n== stage 4/7: embedding ==")
    cfg, embedded = stage_embed(rep, msgs, args)

    print("\n== stage 5/7: pairwise similarity ==")
    details = stage_pairs(rep, [m for m in embedded if m.vector is not None], args)

    print("\n== stage 6/7: judge sample ==")
    judge_results = stage_judge(rep, cfg, details, args)

    print("\n== stage 7/7: report ==")
    stage_checklist(rep, msgs, details, judge_results, args.days)
    path = write_report(rep, args)
    print(f"\nreport written: {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
