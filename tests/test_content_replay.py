import copy
import pytest
from chat_daily_tg.content_replay import freeze, replay, verify, write_frozen
from chat_daily_tg.call_receipts import digest


def samples():
    return [dict(sample_id=str(i),content_id=str(i),source_ref='https://t.me/example/'+str(i),
                 text='source '+str(i),collected_at='2026-09-29T00:00:00+00:00',event_group=str(i),
                 split='development' if i<2 else 'holdout',human_label='worth_sending',
                 annotator='fixture',label_reason='test only') for i in range(3)]


def rules():
    return [dict(version=v,prompt_hash=digest(v),schema_version='v1',rank_version='v1') for v in ('a','b')]


def test_freeze_integrity_and_no_leak(tmp_path):
    rows=samples();m=freeze(rows,version='v1',split_version='v1')
    path=tmp_path/'samples.json';write_frozen(path,m);write_frozen(path,m)
    bad=copy.deepcopy(m);bad['samples'][0]['text']='changed'
    with pytest.raises(ValueError):verify(bad)
    rows[2]['event_group']='0'
    with pytest.raises(ValueError,match='leaks'):freeze(rows,version='v1',split_version='v1')


def test_denominators_disagreements_and_failure():
    m=freeze(samples(),version='v1',split_version='v1')
    def evaluate(s,r):
        if s['sample_id']=='1':raise RuntimeError('private error')
        return dict(decision='include' if r['version']=='a' else 'omit',model='test')
    r=replay(m,rules=rules(),evaluate=evaluate)
    assert len(r['disagreements'])==1
    assert r['metrics']['a']['retention']==dict(included=1,labelled_total=2,evaluated_total=1,rate=1)
    assert r['metrics']['a']['failures']==1
    with pytest.raises(ValueError,match='contract'):replay(m,rules=rules(),evaluate=evaluate,split='holdout')


def test_labels_require_actual_attribution():
    rows=samples();rows[0].pop('annotator')
    with pytest.raises(ValueError):freeze(rows,version='v1',split_version='v1')


def test_model_replay_keeps_raw_failures_and_original_samples(tmp_path):
    import json
    from chat_daily_tg.content_replay import model_replay
    m=freeze(samples(),version='v1',split_version='v1')
    before=copy.deepcopy(m)
    rs=[{**r,'text':r['version']} for r in rules()]
    class Model:
        model='fixture'
        def __init__(self):self.calls=0
        def chat(self,prompt,system):
            self.calls+=1
            sample=json.loads(prompt)
            if self.calls==2:return 'invalid JSON',{}
            return json.dumps({'decision':'include','reason':'test','quote':sample['original']}),{}
    model=Model()
    result=model_replay(m,rules=rs,llm=model,output_root=tmp_path)
    assert model.calls==4 and m==before
    assert result['rows'][1]['status']=='failed'
    assert result['calls'][1]['response_hash']==digest('invalid JSON')
    from pathlib import Path
    assert Path(result['calls'][1]['response_path']).read_text()=='invalid JSON'
    assert Path(result['report_path']).exists()
    assert Path(result['report_path']).stat().st_mode & 0o777 == 0o600


def test_model_replay_checks_holdout_before_network(tmp_path):
    from chat_daily_tg.content_replay import model_replay
    class Model:
        def chat(self,*a,**k):raise AssertionError('must not call')
    m=freeze(samples(),version='v1',split_version='v1')
    with pytest.raises(ValueError,match='contract'):
        model_replay(m,rules=[{**r,'text':r['version']} for r in rules()],llm=Model(),
                     output_root=tmp_path/'out',split='holdout')
    assert not (tmp_path/'out').exists()
