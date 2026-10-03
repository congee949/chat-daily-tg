"""Best-effort acquisition receipts, separate from duration-only task logs."""
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import socket
import uuid

log=logging.getLogger(__name__)


def record_fetch(path, *, producer, source_ref, started_at, status, count=None, latest_content_at=None, error_type=None, count_basis="source_rows"):
    """A successful receipt requires an authoritative source count, never elapsed time."""
    try:
        if status not in {'failed','success','no_update','parsed_empty','sync_completed'}:
            raise ValueError('invalid acquisition status')
        if status not in {'failed','sync_completed'} and (type(count) is not int or count<0):
            raise ValueError('authoritative count required')
        row={'schema':'source-fetch.v1','attempt_id':uuid.uuid4().hex,'machine':socket.gethostname(),
             'producer':producer,'source_ref':source_ref,'started_at':started_at,
             'finished_at':datetime.now(timezone.utc).isoformat(),'status':status,'source_count':count,
             'latest_content_at':latest_content_at,'error_type':error_type,'count_basis':count_basis}
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'a',encoding='utf-8') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        return row['attempt_id']
    except Exception as exc:
        log.warning('fetch health receipt unavailable error_type=%s',type(exc).__name__)
        return None


def fetch_started():
    return datetime.now(timezone.utc).isoformat()


def record_seen_fetch(seen, **fields):
    """Keep acquisition evidence alongside the caller's isolated seen root."""
    try:
        path=Path(seen.path).parent/'fetch_health.jsonl'
        return record_fetch(path,**fields)
    except Exception as exc:
        log.warning('fetch health path unavailable error_type=%s',type(exc).__name__)
        return None
