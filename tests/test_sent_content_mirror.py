from datetime import datetime, timedelta, timezone
import hashlib
import json
import pytest
from chat_daily_tg.sent_content_mirror import write_snapshot, read_snapshot
from chat_daily_tg.topic_dedup import DeliveredIndex
from chat_daily_tg.evidence_index import EmbeddingGeneration
TEST_GENERATION = EmbeddingGeneration(generation_id="mirror-test-v1", model_id="qwen-test",
    model_revision="weights", dimension=2, normalized=True,
    query_template="query-v1", document_template="document-v1", symmetric_query_document=True)
class FakeEmbedder:
    generation = TEST_GENERATION
    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


def row(mid=1, chat=-100123, text='官方已经确认模型发布，包含价格和使用范围等完整信息。'):
    return dict(schema='sent-content.v1', delivery_state='confirmed', producer='x_monitor',
                chat_id=chat, message_id=mid, thread_id=19, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                sent_at=datetime.now(timezone.utc).isoformat())


def snapshot(tmp_path, rows):
    p=tmp_path/'mirror.json'
    write_snapshot(('\n'.join(json.dumps(r) for r in rows)).encode(), p, source='fixture')
    return p


def test_caption_import_preserves_hwm_normalizes_offset_and_filters_chat(tmp_path):
    r=row();r['sent_at']=datetime.now(timezone(timedelta(hours=8))).isoformat()
    p=snapshot(tmp_path,[r,row(2,chat=-100456)])
    idx=DeliveredIndex(tmp_path/'index.db',generation=TEST_GENERATION)
    idx._conn.execute("INSERT INTO meta VALUES ('hwm','999')");idx._conn.commit()
    assert idx.ingest_sent_ledger(p,-100123)==1
    record=idx._conn.execute('SELECT * FROM delivered').fetchone()
    assert record['text']==r['content'] and record['ts'].endswith('+00:00')
    assert idx._get_hwm()==999
    assert idx.ingest_sent_ledger(p,-100123)==0
    assert idx.backfill_embeddings(FakeEmbedder())==1
    assert [m.msg_id for m in idx.recent()]==[1]
    idx._conn.execute("UPDATE delivered SET mirror_valid_until='2000-01-01T00:00:00+00:00'");idx._conn.commit()
    assert idx.recent()==[] and idx.coverage(window_hours=48).eligible_rows==0
    assert idx._conn.execute('SELECT count(*) FROM delivered').fetchone()[0]==1
    idx.close()


@pytest.mark.parametrize('field,value',[('content_hash','bad'),('delivery_state','ambiguous'),('message_id',True),('thread_id',0),('producer','unknown')])
def test_invalid_source_preserves_last_good(tmp_path,field,value):
    rows=[row()];p=snapshot(tmp_path,rows);before=p.read_bytes()
    broken=dict(rows[0]);broken[field]=value
    with pytest.raises(ValueError):write_snapshot(json.dumps(broken).encode(),p,source='fixture')
    assert p.read_bytes()==before


def test_shrinking_or_rewriting_source_keeps_last_good(tmp_path):
    rows=[row(),row(2)];p=snapshot(tmp_path,rows);before=p.read_bytes()
    with pytest.raises(ValueError):write_snapshot(json.dumps(rows[0]).encode(),p,source='fixture')
    assert p.read_bytes()==before


def test_stale_snapshot_never_imports(tmp_path):
    p=snapshot(tmp_path,[row()]);value=json.loads(p.read_text())
    value['fetched_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    p.write_text(json.dumps(value))
    with pytest.raises(ValueError):read_snapshot(p)
    idx=DeliveredIndex(tmp_path/'index.db',generation=TEST_GENERATION)
    assert idx.ingest_sent_ledger(p,-100123)==0
    assert idx._conn.execute('SELECT count(*) FROM delivered').fetchone()[0]==0
    idx.close()
