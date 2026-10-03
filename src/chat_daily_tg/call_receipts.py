"""Private provider receipts and validated-response cache; no delivery state writes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from chat_daily_tg.logging_setup import redact


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def redact_value(value):
    """Redact values before serialization so redaction cannot corrupt JSON syntax."""
    if isinstance(value, dict):
        return {str(key): ('<REDACTED_SECRET>' if str(key).casefold().replace('-', '_') in
                {'authorization', 'cookie', 'set_cookie', 'api_key', 'token', 'access_token', 'password'}
                else redact_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return redact(value) if isinstance(value, str) else value


class CallReceipts:
    """Cache entries become visible only in the successful receipt transaction.

    Raw provider responses stay in a mode-0600 SQLite database. Receipts contain
    hashes and metadata only. A per-key nonblocking lock avoids waiting on other
    workers; a busy key is evaluated live without cache publication.
    """
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'calls.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS responses (
                    cache_key TEXT PRIMARY KEY, receipt_id TEXT NOT NULL,
                    created REAL NOT NULL, response TEXT NOT NULL, response_hash TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(*, service, model, state, questions, policy):
        return digest({'schema': 'provider-response.v1', 'service': service,
                       'model': model, 'input': state, 'questions': questions, 'policy': policy})

    @contextmanager
    def lock(self, key):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid cache key')
        fd = os.open(self.root / (key + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def append(self, request_id, status, metadata, *, response=None, key=None):
        identity = uuid.uuid4().hex
        created = time.time()
        safe = redact_value(metadata)
        json.dumps(safe, allow_nan=False)
        safe.update(receipt_id=identity, started_or_finished_at=datetime.now(timezone.utc).isoformat())
        with self.connect() as db:
            db.execute('INSERT INTO receipts VALUES (?,?,?,?,?)',
                       (identity, request_id, status, created, json.dumps(safe, ensure_ascii=False)))
            if response is not None:
                if status != 'succeeded' or not key:
                    raise ValueError('only successful responses may be cached')
                encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
                db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?)',
                           (key, identity, created, encoded, digest(response)))
        return identity

    def read(self, key, ttl):
        if ttl <= 0:
            return None
        with self.connect() as db:
            row = db.execute('''SELECT r.receipt_id,r.created,r.response,r.response_hash
                FROM responses r JOIN receipts s ON s.id=r.receipt_id
                WHERE r.cache_key=? AND s.status='succeeded' ''', (key,)).fetchone()
        if not row or not 0 <= time.time() - row[1] <= ttl:
            return None
        value = json.loads(row[2])
        if digest(value) != row[3]:
            return None
        return row[0], value

    def archive_raw(self, request_id, attempt_id, body):
        """Persist exact HTTP entity bytes, never headers or credentials."""
        if not isinstance(body, bytes):
            raise ValueError('raw response must be bytes')
        raw_hash = hashlib.sha256(body).hexdigest()
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS raw_responses (
                request_id TEXT NOT NULL, attempt_id TEXT PRIMARY KEY,
                body BLOB NOT NULL, body_hash TEXT NOT NULL)''')
            db.execute('INSERT INTO raw_responses VALUES (?,?,?,?)',
                       (request_id, attempt_id, body, raw_hash))
        return {'raw_response_ref': attempt_id, 'raw_response_sha256': raw_hash,
                'raw_response_bytes': len(body)}

    def report(self, *, stale_after_seconds=300, now=None):
        """Infer interrupted calls without changing receipts or retrying requests."""
        now = time.time() if now is None else now
        if stale_after_seconds < 0:
            raise ValueError('negative stale interval')
        with self.connect() as db:
            rows = db.execute('SELECT request_id,status,created,metadata FROM receipts ORDER BY created,id').fetchall()
        attempts = {}; reused = []; logical = {}
        for request_id,status,created,encoded in rows:
            metadata = json.loads(encoded)
            logical.setdefault(request_id, []).append((status,metadata))
            attempt_id = metadata.get('attempt_id')
            if attempt_id:
                item = attempts.setdefault((request_id,attempt_id), {
                    'request_id':request_id,'attempt_id':attempt_id,'started_at':None,'status':'unknown'})
                if status == 'started':item['started_at']=created
                elif status in {'succeeded','failed','unknown'}:
                    item.update(status=status,error_type=metadata.get('error_type'),http_status=metadata.get('http_status'))
            if metadata.get('origin')=='reused' and status=='succeeded':reused.append(metadata)
        unresolved=[]
        for item in attempts.values():
            if item['status']=='unknown':
                age=now-item['started_at'] if item['started_at'] is not None else None
                unresolved.append({**item,'age_seconds':age,
                    'review_due':age is not None and age >= stale_after_seconds})
        failure_types={}
        for item in attempts.values():
            if item['status'] in {'failed','unknown'}:
                kind=item.get('error_type') or (f"http_{item['http_status']}" if item.get('http_status') else 'no_reliable_terminal')
                failure_types[kind]=failure_types.get(kind,0)+1
        live_summaries=[m for entries in logical.values() for status,m in entries
                        if status=='succeeded' and m.get('origin')=='live' and not m.get('attempt_id')]
        return {'schema':'provider-receipt-report.v1','logical_requests':len(logical),
            'network_attempts':sum(i['started_at'] is not None for i in attempts.values()),
            'successful_reuses':len(reused),'failure_types':failure_types,'unknown_attempts':unresolved,
            'live_latency_ms':[m['latency_ms'] for m in live_summaries if 'latency_ms' in m],
            'accounting_scope':'persisted receipts; failed receipt writes are not counted'}


    def export_response(self, *, attempt_id):
        """Export an explicitly selected response with hash provenance and redaction.

        Free-form prose can still contain personal information. This artifact is
        a local review input, not an automatically approved public fixture.
        """
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_responses'").fetchone():
                raise ValueError('no archived response')
            row = db.execute('SELECT request_id,body,body_hash FROM raw_responses WHERE attempt_id=?',
                             (attempt_id,)).fetchone()
        if row is None:
            raise ValueError('unknown attempt id')
        request_id, body, raw_hash = row
        if hashlib.sha256(body).hexdigest() != raw_hash:
            raise ValueError('raw response hash mismatch')
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeError):
            raise ValueError('non-JSON response needs separate manual review') from None
        sanitized = redact_value(parsed)
        return {'schema':'provider-response-export.v1','request_id':request_id,
                'attempt_id':attempt_id,'raw_response_sha256':raw_hash,
                'redacted_response_sha256':digest(sanitized),'response':sanitized,
                'redaction_policy':'logging-redact+credential-fields.v1',
                'privacy_review_required':True}
