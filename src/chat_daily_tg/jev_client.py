"""Typed, bounded TypeSafe System One HTTP client for Jev."""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from contextlib import nullcontext
from pathlib import Path
import uuid
import httpx

log = logging.getLogger(__name__)

class JevError(RuntimeError):
    def __init__(self, kind, *, attempts=0, status_code=None):
        super().__init__(kind)
        self.kind, self.attempts, self.status_code = kind, attempts, status_code

@dataclass(frozen=True)
class JevResponse:
    model: str
    answers: dict[str, dict]
    usage: dict[str, object]
    provider_metadata: dict[str, object]
    latency_ms: int
    attempts: int = 1
    request_id: str = ''
    origin: str = 'live'
    receipt_id: str = ''

def probability(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError('invalid_probability')
    return float(value)

@dataclass
class JevClient:
    api_key: str = field(repr=False)
    endpoint: str = field(default_factory=lambda: os.environ.get('TYPESAFE_BASE_URL', 'https://api.typesafe.ai/v1').rstrip('/') + '/systemone')
    model: str = 'jev-latest'
    timeout: float = 3
    retry_max_attempts: int = 2
    zero_data_retention: bool = False
    client: httpx.Client | None = field(default=None, repr=False)
    receipt_root: Path | None = None
    cache_ttl_seconds: float = 300
    policy_version: str = 'same-event.v1'
    _owned: bool = field(default=False, init=False)
    _receipt_context: object = field(default=None, init=False, repr=False)
    _attempt_id: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        if self.endpoint != 'https://api.typesafe.ai/v1/systemone' or self.model != 'jev-latest':
            raise ValueError('unsupported Jev endpoint/model')
        if not self.api_key or not 0 < self.timeout <= 3 or self.retry_max_attempts not in (1, 2) or self.zero_data_retention:
            raise ValueError('invalid Jev policy')

    def close(self):
        if self._owned and self.client is not None:
            self.client.close()
            self.client = None

    def __del__(self):
        self.close()

    def _receipt(self, status, **metadata):
        if self._receipt_context is None:
            return ''
        store, logical_id, base = self._receipt_context
        try:
            return store.append(logical_id, status, {**base, **metadata})
        except Exception as exc:
            log.warning('Jev receipt unavailable error_type=%s', type(exc).__name__)
            return ''

    def evaluate(self, *, state: dict, questions: dict, bypass_cache=False) -> JevResponse:
        from chat_daily_tg.call_receipts import CallReceipts, digest
        store = None
        cache_locked = False
        lock = nullcontext(False)
        logical_id = uuid.uuid4().hex
        base = dict(stage='jev.evaluate', model=self.model, input_hash=digest(state),
                    schema_hash=digest(questions), policy_version=self.policy_version)
        key = CallReceipts.key(service=self.endpoint, model=self.model, state=state,
                               questions=questions, policy=self.policy_version)
        try:
            if self.receipt_root is not None:
                store = CallReceipts(self.receipt_root)
                lock = store.lock(key)
                cache_locked = lock.__enter__()
        except Exception as exc:
            log.warning('Jev receipt storage unavailable error_type=%s', type(exc).__name__)
            store = None
            lock = nullcontext(False)
        self._receipt_context = (store, logical_id, base) if store else None
        started = time.monotonic()
        try:
            if store and cache_locked and not bypass_cache:
                try:
                    cached = store.read(key, min(300, self.cache_ttl_seconds))
                    if cached:
                        receipt, data = cached
                        # Cache stores the already validated normalized JevResponse.
                        self._validate_cached(data, questions)
                        rid = self._receipt('succeeded', origin='reused', original_receipt_id=receipt,
                                            attempts=0, usage=None, usage_reason='no network call')
                        return JevResponse(**{**data, 'attempts': 0, 'usage': {},
                                              'latency_ms': 0, 'origin': 'reused', 'receipt_id': rid})
                except Exception as exc:
                    log.warning('Jev cache read unavailable error_type=%s', type(exc).__name__)
            result = self._evaluate_live(state=state, questions=questions)
            if store:
                try:
                    rid = store.append(logical_id, 'succeeded', {**base, 'origin': 'live',
                        'usage': result.usage, 'provider_request_id': result.request_id or None,
                        'attempts': result.attempts, 'latency_ms': result.latency_ms},
                        response=asdict(result) if cache_locked else None, key=key if cache_locked else None)
                    from dataclasses import replace
                    result = replace(result, receipt_id=rid)
                except Exception as exc:
                    log.warning('Jev cache write unavailable error_type=%s', type(exc).__name__)
            return result
        except JevError as exc:
            self._receipt('unknown' if exc.kind in {'timeout', 'transport'} else 'failed',
                          origin='live', error_type=exc.kind, attempts=exc.attempts, attempt_id=self._attempt_id,
                          latency_ms=round((time.monotonic()-started)*1000))
            raise
        finally:
            self._receipt_context = None
            lock.__exit__(None, None, None)

    @staticmethod
    def _validate_cached(data, questions):
        if not isinstance(data, dict) or not str(data.get('model', '')).startswith('jev-'):
            raise ValueError('invalid cached model')
        answers = data['answers']
        if set(answers) != set(questions):
            raise ValueError('invalid cached questions')
        for key, q in questions.items():
            a = answers[key]
            if q['type'] in {'boolean', 'noul'}:
                if a.get('type') != 'noul' or probability(a['noul']) != probability(a['probability']):
                    raise ValueError('invalid cached boolean')
            elif q['type'] == 'choice':
                if a.get('type') != 'choice':
                    raise ValueError('invalid cached answer type')
                probability(a['confidence'])
                if set(a['probabilities']) != set(q['criteria']) or a['choice'] not in q['criteria']:
                    raise ValueError('invalid cached choice')
                if abs(sum(probability(v) for v in a['probabilities'].values()) - 1) > .01:
                    raise ValueError('invalid cached probabilities')
            else:
                raise ValueError('invalid cached question type')

    def _evaluate_live(self, *, state: dict, questions: dict) -> JevResponse:
        wire_questions = {key: {**q, 'type': 'noul' if q['type'] == 'boolean' else q['type']} for key, q in questions.items()}
        payload = dict(model=self.model, state=state, questions=wire_questions)
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)
            self._owned = True
        started = time.monotonic()
        for attempt in range(1, self.retry_max_attempts + 1):
            attempt_id = uuid.uuid4().hex
            self._attempt_id = attempt_id
            self._receipt('started', origin='live', attempt_id=attempt_id, attempt=attempt)
            try:
                response = self.client.post(self.endpoint, json=payload,
                    headers={'Authorization': 'Bearer ' + self.api_key}, timeout=self.timeout)
            except httpx.TimeoutException:
                raise JevError('timeout', attempts=attempt) from None
            except httpx.HTTPError:
                raise JevError('transport', attempts=attempt) from None
            if self._receipt_context is not None:
                store, logical_id, _base = self._receipt_context
                try:
                    raw = store.archive_raw(logical_id, attempt_id, response.content)
                    self._receipt('response_received', origin='live', attempt_id=attempt_id, **raw)
                except Exception as exc:
                    self._receipt('response_archive_failed', origin='live', attempt_id=attempt_id,
                                  error_type=type(exc).__name__)
            status = response.status_code
            if status != 200:
                self._receipt('failed', origin='live', attempt_id=attempt_id,
                              attempt=attempt, http_status=status,
                              provider_request_id=response.headers.get('x-request-id') or None)
            if status == 429 or 500 <= status <= 599:
                if attempt < self.retry_max_attempts:
                    time.sleep(0.1)
                    continue
            if status != 200:
                raise JevError('http_error', attempts=attempt, status_code=status)
            try:
                data = response.json()
                answers = data['answers']
                if not isinstance(answers, dict) or set(answers) != set(questions):
                    raise ValueError()
                for key, question in questions.items():
                    answer = answers[key]
                    if not isinstance(answer, dict):
                        raise ValueError()
                    if question['type'] in {'boolean', 'noul'}:
                        if answer.get('type') != 'noul':
                            raise ValueError()
                        answer['probability'] = probability(answer['noul'])
                    elif question['type'] == 'choice':
                        if answer.get('type') != 'choice':
                            raise ValueError()
                        probability(answer['confidence'])
                        probs = answer['probabilities']
                        if not isinstance(probs, dict) or set(probs) != set(question['criteria']) or answer['choice'] not in probs:
                            raise ValueError()
                        if abs(sum(probability(v) for v in probs.values()) - 1) > 0.01:
                            raise ValueError()
                    else:
                        raise ValueError()
                if not isinstance(data.get('model'), str) or not data['model'].startswith('jev-') or not isinstance(data.get('usage'), dict):
                    raise ValueError()
                for name in ('input_tokens', 'output_tokens'):
                    if type(data['usage'].get(name)) is not int or data['usage'][name] < 0:
                        raise ValueError()
            except (KeyError, ValueError, TypeError, JevError):
                raise JevError('malformed_response', attempts=attempt) from None
            self._receipt('succeeded', origin='live', attempt_id=attempt_id,
                          attempt=attempt, http_status=status,
                          provider_request_id=response.headers.get('x-request-id') or None)
            latency = round((time.monotonic() - started) * 1000)
            request_id = response.headers.get('x-request-id', '')
            log.info('Jev model=%s status=ok latency_ms=%s request_id=%s input_sha256=%s', self.model, latency, request_id or '-', digest)
            usage = data['usage']
            normalized_usage = {
                'inputTokens': usage.get('input_tokens', usage.get('inputTokens', 0)),
                'outputTokens': usage.get('output_tokens', usage.get('outputTokens', 0)),
            }
            normalized_usage['totalTokens'] = normalized_usage['inputTokens'] + normalized_usage['outputTokens']
            return JevResponse(data['model'], answers, normalized_usage, {}, latency, attempt, request_id)
        raise JevError('exhausted')
