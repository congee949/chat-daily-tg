import json
import socket
import sqlite3
from chat_daily_tg.content_health import collect_health
import pytest


def test_expired_claim_observed_without_modifying_db(tmp_path):
    path=tmp_path/'db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE growth_segments(id TEXT,status TEXT,claim_until TEXT)')
        db.execute("INSERT INTO growth_segments VALUES ('a','sending','2026-09-28T00:00:00+00:00')")
    before=path.read_bytes()
    result=collect_health(machine=socket.gethostname(),growth_db=path,now='2026-09-29T00:00:00+00:00')
    assert result['observations'][0]['status']=='active'
    assert result['observations'][0]['evidence']['expired_claim_ids']==['a']
    assert path.read_bytes()==before
    with pytest.raises(ValueError,match='execution host'):collect_health(machine='different-host',growth_db=path)


def test_fresh_judge_recovery_but_stale_or_truncated_input_unknown(tmp_path):
    path=tmp_path/'jev.jsonl'
    row={'schema':'chatdaily.jev-dedup-judge.v1','timestamp':'2026-09-29T00:00:00+00:00','status':'ok','fallback_used':False}
    path.write_text(json.dumps(row)+'\n')
    args=dict(machine=socket.gethostname(),jev_journal=path,now='2026-09-29T01:00:00+00:00')
    result=collect_health(**args)
    assert result['observations'][0]['status']=='recovered'
    path.write_text(json.dumps(row)+'\n{broken')
    result=collect_health(**args)
    assert len(result['observations'])==1 and result['observations'][0]['kind']=='health_input'
    path.write_text(json.dumps(row)+'\n')
    result=collect_health(**{**args,'now':'2026-10-01T00:00:00+00:00'})
    assert result['observations'][0]['status']=='active'


def test_missing_db_remains_missing(tmp_path):
    path=tmp_path/'missing'
    result=collect_health(machine=socket.gethostname(),growth_db=path)
    assert result['observations'][0]['kind']=='health_input'
    assert not path.exists()


def test_stale_success_retains_timestamp_and_does_not_report_model_failure(tmp_path):
    path=tmp_path/'jev.jsonl'
    path.write_text(json.dumps({'schema':'chatdaily.jev-dedup-judge.v1','timestamp':'2026-09-25T04:06:10+00:00',
                               'status':'ok','fallback_used':False})+'\n')
    result=collect_health(machine=socket.gethostname(),jev_journal=path,now='2026-09-29T04:06:10+00:00')
    finding=result['observations'][0]
    assert finding['kind']=='health_input'
    assert finding['evidence']['reason_code']=='stale_judge_receipt'
    assert finding['evidence']['latest_recorded_status']=='ok'
    assert finding['evidence']['age_seconds']==4*86400
    assert not any(o['kind']=='model_degradation' for o in result['observations'])
