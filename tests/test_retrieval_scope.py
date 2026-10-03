import sqlite3
import pytest
from chat_daily_tg.knowledge_index import GenerationReader,retrieval_scope_sql


def test_scope_applies_before_exact_and_fts_limits():
    reader=GenerationReader.__new__(GenerationReader)
    reader.conn=sqlite3.connect(':memory:');reader.conn.row_factory=sqlite3.Row
    reader.conn.executescript('''CREATE TABLE content_items(content_id TEXT,active INT,published_at TEXT);
    CREATE TABLE chunks(chunk_id TEXT,content_id TEXT,active INT);
    CREATE TABLE exact_terms(chunk_id TEXT,content_id TEXT,term TEXT,kind TEXT);
    CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED,text);''')
    for i in range(80):
        cid=f'c{i}';chunk=f'chunk{i}'
        reader.conn.execute('INSERT INTO content_items VALUES (?,1,?)',(cid,'2026-09-29T00:00:00+00:00' if i==79 else '2026-01-01T00:00:00+00:00'))
        reader.conn.execute('INSERT INTO chunks VALUES (?,?,1)',(chunk,cid))
        reader.conn.execute('INSERT INTO exact_terms VALUES (?,?,?,?)',(chunk,cid,'needle','url'))
        reader.conn.execute('INSERT INTO chunk_fts VALUES (?,?)',(chunk,'needle'))
    for lookup in (reader._exact,reader._lexical):
        assert lookup('needle',1,scope={'content_ids':['c79']})==['chunk79']
        assert lookup('needle',1,scope={'content_ids':[]})==[]
        assert lookup('needle',1,scope={'published_after':'2026-09-28T00:00:00+00:00'})==['chunk79']
    reader.conn.close()


def test_scope_uses_bound_values_and_rejects_unsupported_fields():
    sql,params=retrieval_scope_sql({'content_ids':["a' OR 1=1 --"]})
    assert 'OR 1=1' not in sql and 'OR 1=1' in params[0]
    with pytest.raises(ValueError):retrieval_scope_sql({'arbitrary_sql':'x'})
    with pytest.raises(ValueError):retrieval_scope_sql({'published_after':'2026-09-29'})


def test_task_sets_scope_before_diagnostic_query(tmp_path,monkeypatch,capsys):
    import json
    from types import SimpleNamespace
    from chat_daily_tg import knowledge_cli as cli
    feedback=tmp_path/'feedback';feedback.write_text(json.dumps({'event_type':'read','content_id':'read-item'})+'\n')
    ledger=tmp_path/'ledger';ledger.write_text(json.dumps({'delivery_state':'confirmed','content_id':'delivered-item'})+'\n')
    seen=[]
    def query(args):
        seen.append(args._content_scope)
        print(json.dumps({'result':{'hits':[{'content_id':'read-item','text':'original'}]}}))
    monkeypatch.setattr(cli,'cmd_diagnostic_query',query)
    args=SimpleNamespace(feedback=feedback,delivered_ledger=ledger,task='recall',timeout=8,
        diagnostic=True,generation='fixture',event_root=None,expand_archive=False)
    assert cli.cmd_task(args)==0
    result=json.loads(capsys.readouterr().out)
    assert seen==[{'content_ids':['read-item']}]
    assert result['scope_filter']=='applied before exact/FTS/dense candidate limits'
    assert result['results'][0]['content_id']=='read-item'
