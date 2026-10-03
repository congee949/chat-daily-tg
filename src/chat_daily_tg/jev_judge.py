"""TypeSafe same-event judge with chat-judge fallback and result journaling."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import time

from chat_daily_tg.jev_client import JevClient, probability
from chat_daily_tg.jev_shadow import QUESTIONS, input_hash, make_state
from chat_daily_tg.topic_dedup import JudgeVerdict

log = logging.getLogger(__name__)


class JevJudge:
    model = "jev-latest"

    def __init__(self, client, *, path: Path, fallback=None, threshold=0.5):
        if not 0 < threshold < 1:
            raise ValueError("invalid Jev judge policy")
        self.client, self.path, self.fallback = client, Path(path), fallback
        self.threshold = threshold

    def _fallback(self, text, matches, cause):
        if self.fallback is not None:
            try:
                verdict = self.fallback.judge(text, matches)
                # Bind provenance to the actual judge; do not copy LLM prose to logs.
                return replace(verdict, reason=f"jev-fallback:{cause}")
            except Exception as exc:
                log.warning("Jev fallback failed error_type=%s", type(exc).__name__)
        return JudgeVerdict(False, "substantial", f"jev-unavailable:{cause}", False)

    def _record(self, row):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        except Exception as exc:
            log.warning("Jev judge journal unavailable error_type=%s", type(exc).__name__)

    def judge(self, text, matches):
        started = time.monotonic()
        row = {
            "schema": "chatdaily.jev-dedup-judge.v1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "candidate_msg_ids": [m.msg_id for m in matches],
            "status": "error", "fallback_used": False, "attempts": 0,
        }
        cause = "error"
        try:
            state = make_state(text, matches)
            row["input_sha256"] = input_hash(state)
            result = self.client.evaluate(state=state, questions=QUESTIONS)
            same_probability = probability(result.answers["same_event"]["probability"])
            info = result.answers["new_info"]
            new_info = info["choice"]
            if new_info not in {"none", "minor", "substantial", "uncertain"}:
                raise ValueError("invalid Jev new_info")
            row.update(
                model=result.model, attempts=result.attempts,
                usage=result.usage, request_id=result.request_id,
                same_event_probability=same_probability, new_info=new_info,
                threshold=self.threshold,
            )
            if new_info == "uncertain":
                cause = "uncertain"
                row["status"] = cause
            else:
                row["status"] = "ok"
                verdict = JudgeVerdict(
                    same_probability >= self.threshold, new_info,
                    f"jev:probability={same_probability:.3f}", True,
                )
        except Exception as exc:
            cause = getattr(exc, "kind", type(exc).__name__)
            row.update(error_type=cause, attempts=getattr(exc, "attempts", 0),
                       http_status=getattr(exc, "status_code", None))
        if row["status"] != "ok":
            row["fallback_used"] = self.fallback is not None
            verdict = self._fallback(text, matches, cause)
        row["verdict"] = asdict(verdict)
        row["latency_ms"] = round((time.monotonic() - started) * 1000)
        self._record(row)
        log.info("Jev judge status=%s same_event=%s new_info=%s fallback=%s latency_ms=%s",
                 row["status"], verdict.same_event, verdict.new_info,
                 row["fallback_used"], row["latency_ms"])
        return verdict

    def close(self):
        self.client.close()


def build_jev_judge(cfg, fallback=None):
    topic = cfg.sources.telegram.dedup.topic
    if getattr(topic, "judge_provider", "llm") != "jev":
        return None
    try:
        from chat_daily_tg.config import JevJudgePolicy, JevModel
        from chat_daily_tg.paths import JEV_DEDUP_JUDGE

        model = JevModel.model_validate(cfg.models.jev)
        policy = JevJudgePolicy.model_validate(topic.model_dump())
        if not model.enabled:
            raise ValueError("models.jev disabled")
        client = JevClient(receipt_root=JEV_DEDUP_JUDGE.parent / 'jev-calls', api_key=os.environ.get(model.api_key_env, ""),
                           **model.model_dump(exclude={"api_key_env", "enabled"}))
        return JevJudge(
            client, path=JEV_DEDUP_JUDGE, fallback=fallback,
            threshold=policy.jev_same_event_threshold,
        )
    except Exception as exc:
        log.warning("Jev primary judge unavailable error_type=%s", type(exc).__name__)
        return None
