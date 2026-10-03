import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from chat_daily_tg.call_receipts import CallReceipts
from chat_daily_tg.jev_client import JevClient, JevError

Q = {'same': {'type': 'noul'}}
BODY = {'model': 'jev-1.13.0', 'answers': {'same': {'type': 'noul', 'noul': .8}},
        'usage': {'input_tokens': 2, 'output_tokens': 3}}


def client(root, calls, status=200):
    def handler(req):
        calls.append(1)
        return httpx.Response(status, json=BODY)
    return JevClient('secret', receipt_root=root,
                     client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_reuse_and_bypass(tmp_path):
    calls=[]
    c=client(tmp_path,calls)
    a=c.evaluate(state={'x':1},questions=Q)
    b=c.evaluate(state={'x':1},questions=Q)
    assert a.origin=='live' and a.receipt_id
    assert b.origin=='reused' and b.attempts==0 and b.usage=={}
    c.evaluate(state={'x':1},questions=Q,bypass_cache=True)
    assert len(calls)==2
    c.evaluate(state={'x':2},questions=Q)
    c.policy_version='v2'
    c.evaluate(state={'x':2},questions=Q)
    assert len(calls)==4
    assert (tmp_path/'calls.sqlite3').stat().st_mode & 0o777 == 0o600


def test_failures_and_corruption_not_reused(tmp_path):
    calls=[]
    c=client(tmp_path,calls,401)
    for _ in range(2):
        with pytest.raises(JevError): c.evaluate(state={},questions=Q)
    assert len(calls)==2
    c=client(tmp_path,calls)
    c.evaluate(state={},questions=Q)
    with sqlite3.connect(tmp_path/'calls.sqlite3') as db:
        db.execute("UPDATE responses SET response='{}'")
    assert c.evaluate(state={},questions=Q).origin=='live'


def test_storage_failure_fails_open(tmp_path):
    root=tmp_path/'file';root.write_text('blocked')
    c=client(root,[])
    assert c.evaluate(state={},questions=Q).origin=='live'


def test_concurrent_same_key_and_schema_invalidation(tmp_path):
    calls=[]
    # Initialize schema before racing independent clients, as separate workers do.
    CallReceipts(tmp_path)
    def work(_): return client(tmp_path,calls).evaluate(state={},questions=Q)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(list(pool.map(work,range(8))))==8
    c=client(tmp_path,calls)
    assert c.evaluate(state={},questions=Q).origin=='reused'
    q={'same':{'type':'noul','instructions':'changed'}}
    assert c.evaluate(state={},questions=q).origin=='live'


def test_timeout_unknown_and_unfinished_started_preserved(tmp_path):
    def fail(req): raise httpx.ReadTimeout('secret body')
    c=JevClient('secret',receipt_root=tmp_path,client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(JevError): c.evaluate(state={},questions=Q)
    with sqlite3.connect(tmp_path/'calls.sqlite3') as db:
        rows=db.execute('SELECT status,metadata FROM receipts').fetchall()
        assert [r[0] for r in rows]==['started','unknown']
        assert db.execute('SELECT COUNT(*) FROM responses').fetchone()[0]==0
    assert 'secret' not in json.dumps(rows)


def test_cache_schema_validation_even_with_matching_checksum(tmp_path):
    from chat_daily_tg.call_receipts import digest
    calls=[];c=client(tmp_path,calls)
    c.evaluate(state={},questions=Q)
    with sqlite3.connect(tmp_path/'calls.sqlite3') as db:
        data=json.loads(db.execute('SELECT response FROM responses').fetchone()[0])
        data['answers']['same']['type']='choice'
        db.execute('UPDATE responses SET response=?,response_hash=?',(json.dumps(data),digest(data)))
    assert c.evaluate(state={},questions=Q).origin=='live'
    assert len(calls)==2


def test_raw_response_and_network_accounting(tmp_path):
    calls=[];c=client(tmp_path,calls)
    c.evaluate(state={},questions=Q)
    c.evaluate(state={},questions=Q)
    store=CallReceipts(tmp_path)
    report=store.report()
    assert report['network_attempts']==1
    assert report['successful_reuses']==1
    assert report['unknown_attempts']==[]
    with store.connect() as db:
        body,checksum=db.execute('SELECT body,body_hash FROM raw_responses').fetchone()
    import hashlib
    assert json.loads(body)==BODY
    assert hashlib.sha256(body).hexdigest()==checksum


def test_interrupted_attempt_is_reviewed_without_mutation(tmp_path):
    store=CallReceipts(tmp_path)
    store.append('logical','started',{'attempt_id':'interrupted','origin':'live'})
    with store.connect() as db:
        started=db.execute('SELECT created FROM receipts').fetchone()[0]
    report=store.report(now=started+301)
    assert report['unknown_attempts'][0]['review_due'] is True
    assert report['failure_types']=={'no_reliable_terminal':1}
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]==1


def test_raw_archive_failure_preserves_model_result(tmp_path,monkeypatch):
    def fail(*args,**kwargs):raise OSError('disk full')
    monkeypatch.setattr(CallReceipts,'archive_raw',fail)
    assert client(tmp_path,[]).evaluate(state={},questions=Q).origin=='live'


def test_receipt_redaction_preserves_json_and_masks_nested_fields(tmp_path):
    store=CallReceipts(tmp_path)
    store.append('request','failed',{'error':'Cookie: session=private',
        'nested':{'api-key':'sensitive','authorization':'Bearer private'}})
    with store.connect() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM receipts').fetchone()[0])
    assert metadata['error']=='Cookie: <REDACTED_SECRET>'
    assert metadata['nested']=={'api-key':'<REDACTED_SECRET>','authorization':'<REDACTED_SECRET>'}


def test_selected_raw_export_redacts_and_preserves_original(tmp_path):
    store=CallReceipts(tmp_path)
    body=json.dumps({'token':'private-token','answers':{'text':'Cookie: session=private'}}).encode()
    store.archive_raw('request','attempt',body)
    exported=store.export_response(attempt_id='attempt')
    assert exported['privacy_review_required'] is True
    assert exported['response']['token']=='<REDACTED_SECRET>'
    assert 'session=private' not in json.dumps(exported)
    with store.connect() as db:
        assert db.execute('SELECT body FROM raw_responses').fetchone()[0]==body
        db.execute("UPDATE raw_responses SET body_hash='corrupt'")
    with pytest.raises(ValueError,match='hash mismatch'):
        store.export_response(attempt_id='attempt')


def test_raw_export_rejects_missing_and_non_json(tmp_path):
    store=CallReceipts(tmp_path)
    with pytest.raises(ValueError):store.export_response(attempt_id='missing')
    store.archive_raw('request','html',b'<html>private error</html>')
    with pytest.raises(ValueError,match='non-JSON'):store.export_response(attempt_id='html')
