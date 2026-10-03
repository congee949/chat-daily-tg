from pathlib import Path
import json
import pytest
from chat_daily_tg import wx_exporter
from types import SimpleNamespace


def test_export_receipt_retains_success_vs_empty_and_failure(tmp_path,monkeypatch):
    output=tmp_path/'archive.md'
    for count,text,status in [(3,'body','success'),(0,'heading','no_update'),(2,'','parsed_empty')]:
        result=SimpleNamespace(message_count=count,content=text)
        monkeypatch.setattr(wx_exporter,'_export_group_impl',lambda *a,**k:result)
        assert wx_exporter.export_group('source','2026-09-28','2026-09-29',output) is result
        row=json.loads((tmp_path/'fetch_health.jsonl').read_text().splitlines()[-1])
        assert row['status']==status and row['source_count']==count
        assert row['latest_content_at'] is None
    def fail(*a,**k):raise RuntimeError('private error')
    monkeypatch.setattr(wx_exporter,'_export_group_impl',fail)
    with pytest.raises(RuntimeError):wx_exporter.export_group('source','a','b',output)
    row=json.loads((tmp_path/'fetch_health.jsonl').read_text().splitlines()[-1])
    assert row['status']=='failed' and row['error_type']=='RuntimeError'
    assert 'private error' not in json.dumps(row)


def test_receipt_storage_failure_preserves_export(tmp_path,monkeypatch):
    (tmp_path/'fetch_health.jsonl').mkdir()
    result=SimpleNamespace(message_count=1,content='body')
    monkeypatch.setattr(wx_exporter,'_export_group_impl',lambda *a,**k:result)
    assert wx_exporter.export_group('source','a','b',tmp_path/'out.md') is result


def test_empty_receipts_are_separate_from_failure_and_source_identity(tmp_path):
    import socket
    from chat_daily_tg.fetch_health import record_fetch
    from chat_daily_tg.content_health import collect_fetch_health
    from chat_daily_tg.content_operations import health_report
    path=tmp_path/'receipts.jsonl'
    for source in ['a','b']:
        for _ in range(3):record_fetch(path,producer='wechat',source_ref=source,started_at='2026-09-29T00:00:00+00:00',status='no_update',count=0)
    result=collect_fetch_health(machine=socket.gethostname(),journals=[path,path])
    health=health_report(result['observations'])
    assert len(health['findings'])==2
    assert all(r['kind']=='consecutive_empty' for r in health['findings'])
    assert all(r['evidence']['consecutive_empty']==3 for r in health['findings'])
    with path.open('a') as f:f.write('{truncated')
    result=collect_fetch_health(machine=socket.gethostname(),journals=[path])
    assert result['input_failures'] and all(o['status']!='recovered' for o in result['observations'])


def test_completed_sync_with_unknown_count_is_not_empty(tmp_path):
    import socket
    from chat_daily_tg.fetch_health import record_fetch
    from chat_daily_tg.content_health import collect_fetch_health
    path=tmp_path/'journal'
    for _ in range(4):
        record_fetch(path,producer='telegram-sync',source_ref='chat',started_at='2026-09-29T00:00:00+00:00',
                     status='sync_completed',count=None,count_basis='unknown')
    report=collect_fetch_health(machine=socket.gethostname(),journals=[path])
    assert report['input_failures']==[]
    assert all(row['evidence']['consecutive_empty']==0 for row in report['observations'])
    assert all(row['status']=='recovered' for row in report['observations'])
