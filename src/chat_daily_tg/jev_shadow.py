"""Sampled, observation-only comparison with result journaling."""
from __future__ import annotations
import fcntl
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from chat_daily_tg.jev_client import JevClient
from chat_daily_tg.sanitize import sanitize_for_llm

log = logging.getLogger(__name__)
QUESTIONS = {
    'same_event': {'type': 'noul', 'instructions': '新卡片是否与至少一条已送达卡片描述同一个具体事件？', 'criteria': {'true': '同一个具体公告、产品事件、资源或事实变化', 'false': '只是同主题、同领域或相似表达，但不是同一个具体事件'}},
    'new_info': {'type': 'choice', 'instructions': '新卡片相对于已送达候选包含多少实质新增信息？', 'criteria': {'none': '基本复述，没有新的事实、数据、来源或操作信息', 'minor': '只补充少量细节，不改变读者判断', 'substantial': '出现新事实、新数据、新来源、新分析或新的操作入口', 'uncertain': '证据不足，无法可靠判断'}},
}

def make_state(text, matches):
    if not 1 <= len(matches) <= 3:
        raise ValueError('one to three candidates required')
    return {'new_card': sanitize_for_llm(text), 'delivered_candidates': [
        {'position': i, 'text': sanitize_for_llm(m.text)} for i, m in enumerate(matches, 1)]}

def input_hash(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

class JevShadow:
    def __init__(self, client, *, path, sample_rate=1.0):
        if not 0 <= sample_rate <= 1:
            raise ValueError('invalid shadow sample rate')
        self.client, self.path = client, Path(path)
        self.sample_rate = sample_rate

    def observe(self, text, matches, verdict, ref=None):
        try:
            if not 1 <= len(matches) <= 3 or random.random() >= self.sample_rate:
                return
            state = make_state(text, matches)
            ref = ref or {}
            row = {'schema': 'chatdaily.jev-dedup-shadow.v1',
                'timestamp': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                'source_chat_id': ref.get('chat_id'), 'source_msg_id': ref.get('msg_id'),
                'candidate_msg_ids': [m.msg_id for m in matches], 'matched_msg_id': matches[0].msg_id,
                'input_sha256': input_hash(state), 'context_truncated': False,
                'current_judge': {'same_event': verdict.same_event, 'new_info': verdict.new_info, 'ok': verdict.ok} if verdict else None}
            started = time.monotonic()
            try:
                result = self.client.evaluate(state=state, questions=QUESTIONS)
                info = result.answers['new_info']
                row.update(status='uncertain' if info['choice'] == 'uncertain' else 'ok',
                    jev={'same_event_probability': result.answers['same_event']['probability'],
                         'new_info': info['choice'], 'confidence': info.get('confidence', info['probabilities'][info['choice']]), 'model': result.model},
                    attempts=result.attempts, usage=result.usage, provider_metadata=result.provider_metadata, request_id=result.request_id)
            except Exception as exc:
                row.update(status='error', error_type=getattr(exc, 'kind', type(exc).__name__),
                    attempts=getattr(exc, 'attempts', 0), http_status=getattr(exc, 'status_code', None))
            row['latency_ms'] = round((time.monotonic() - started) * 1000)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        except Exception as exc:
            log.warning('Jev shadow unavailable error_type=%s', type(exc).__name__)

def build_shadow(cfg):
    topic = cfg.sources.telegram.dedup.topic
    if getattr(topic, 'jev_shadow_enabled', False) is not True:
        return None
    try:
        from chat_daily_tg.config import JevModel, JevPolicy
        from chat_daily_tg.paths import STATE_DIR
        model = JevModel.model_validate(cfg.models.jev)
        policy = JevPolicy.model_validate(topic.model_dump())
        if not model.enabled:
            raise ValueError('models.jev disabled')
        client = JevClient(receipt_root=STATE_DIR / 'jev-calls', api_key=os.environ.get(model.api_key_env, ''), **model.model_dump(exclude={'api_key_env', 'enabled'}))
        return JevShadow(client, path=STATE_DIR / 'jev-dedup-shadow.jsonl',
            sample_rate=policy.jev_shadow_sample_rate)
    except Exception as exc:
        log.warning('Jev shadow initialization disabled error_type=%s', type(exc).__name__)
        return None
