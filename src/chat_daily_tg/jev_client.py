"""Typed, bounded TypeSafe System One HTTP client for Jev."""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
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
    _owned: bool = field(default=False, init=False)

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

    def evaluate(self, *, state: dict, questions: dict) -> JevResponse:
        wire_questions = {key: {**q, 'type': 'noul' if q['type'] == 'boolean' else q['type']} for key, q in questions.items()}
        payload = dict(model=self.model, state=state, questions=wire_questions)
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)
            self._owned = True
        started = time.monotonic()
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                response = self.client.post(self.endpoint, json=payload,
                    headers={'Authorization': 'Bearer ' + self.api_key}, timeout=self.timeout)
            except httpx.TimeoutException:
                raise JevError('timeout', attempts=attempt) from None
            except httpx.HTTPError:
                raise JevError('transport', attempts=attempt) from None
            status = response.status_code
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
