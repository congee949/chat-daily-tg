import hashlib,json
from types import SimpleNamespace
from chat_daily_tg import media_originals as m


def test_catalog_requires_confirmed_original_not_metadata(tmp_path,monkeypatch):
    ledger=[{'producer':'youtube','url':'https://example.org/video'}]
    base=dict(mapping_status='confirmed',document_role='original',representation_type='transcript',
        canonical_url='https://example.org/video',content_id='media:1',title='title',text='complete transcript',metadata={})
    documents=[SimpleNamespace(**base),SimpleNamespace(**{**base,'content_id':'metadata','document_role':'metadata'}),
               SimpleNamespace(**{**base,'content_id':'unmapped','mapping_status':'source_only'})]
    monkeypatch.setattr(m,'load_media_ledger',lambda path:(ledger,{}))
    monkeypatch.setattr(m,'load_podcast',lambda root,rows:(documents,{}))
    path=tmp_path/'originals.jsonl'
    result=m.build_media_originals(podcast_root=tmp_path,media_ledger=tmp_path/'ledger',output_path=path)
    assert result['content_count']==1
    row=json.loads(path.read_text())
    assert row['content_hash']==hashlib.sha256(b'complete transcript').hexdigest()
    assert row['verified_original'] is True and path.stat().st_mode & 0o777==0o600
