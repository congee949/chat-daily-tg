from datetime import datetime,timezone
import json
import pytest

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.rubric_candidates import RubricCandidates
from chat_daily_tg.content_operations import DeliveryReview,health_report
from chat_daily_tg.event_files import EventFiles
from chat_daily_tg.knowledge_tasks import task_results
from chat_daily_tg.value_profiles import rank_candidates
from chat_daily_tg.content_replay import freeze,replay


def candidate_fixture(tmp_path):
    parent='# 成长卡片评审偏好 v1\n'+'parent content '*5
    text='# 成长卡片评审偏好 v2\n'+'candidate content '*5
    active=tmp_path/'rubric.md';active.write_text(parent)
    store=RubricCandidates(tmp_path/'candidates')
    c=store.draft(parent=parent,text=text,feedback_ids=['bot:1'],reason='test')
    samples=[dict(sample_id=str(i),content_id=str(i),source='growth',source_ref='fixture:'+str(i),text='sample'+str(i),
         collected_at='2026-09-29T00:00:00+00:00',event_group=str(i),split='development',
         human_label='worth_sending',annotator='test-fixture',label_reason='fixture',too_long=False,meaning_lost=False,overclaimed=False) for i in range(20)]
    manifest=freeze(samples,version='test',split_version='test')
    rules=[dict(version=v,prompt_hash=digest(t),schema_version='v1',rank_version='v1') for v,t in [('a',parent),('b',text)]]
    report=replay(manifest,rules=rules,evaluate=lambda s,r:{'decision':'omit' if s['sample_id']=='0' and r['version']=='b' else 'include','model':'test'})
    return store,c,active,manifest,report


def test_rubric_review_activation_parent_guard_and_rollback(tmp_path):
    store,c,active,m,r=candidate_fixture(tmp_path)
    with pytest.raises(ValueError):store.activate(c['id'],active)
    store.evaluated(c['id'],m,r)
    with pytest.raises(ValueError,match='explain'):store.review(c['id'],actor='fixture',decision='approve',reason='test',explanations={})
    store.review(c['id'],actor='fixture',decision='approve',reason='test',explanations={'0':'reviewed fixture'})
    active.write_text('changed')
    with pytest.raises(ValueError,match='parent'):store.activate(c['id'],active)
    active.write_text(c['parent_text']);store.activate(c['id'],active)
    assert active.read_text()==c['text']
    store.rollback(active,actor='fixture',reason='test rollback')
    assert active.read_text()==c['parent_text']


def test_failed_evaluation_never_activates(tmp_path):
    store,c,active,m,r=candidate_fixture(tmp_path);r['rows'][0]['status']='failed'
    with pytest.raises(ValueError,match='failed'):store.evaluated(c['id'],m,r)
    assert active.read_text()==c['parent_text']


def test_review_evidence_and_no_implicit_retry(tmp_path):
    store=DeliveryReview(tmp_path)
    key=store.add(content_id='one',attempt_id='try1',machine='mac',producer='growth',requested_at='2026-09-29T00:00:00+00:00',target={'chat_id':1},error_type='ReadTimeout')
    with pytest.raises(ValueError):store.decide(key,decision='retry_requested',actor='fixture',evidence={'reason':'timeout'})
    with pytest.raises(ValueError):store.decide(key,decision='confirmed_sent',actor='fixture',evidence={'reason':'guess'})
    store.decide(key,decision='confirmed_absent',actor='fixture',evidence={'checked_scope':'fixture chat/time'})
    row=store.decide(key,decision='retry_requested',actor='fixture',evidence={'reason':'explicit retry'})
    assert row['state']=='retry_requested' and store.report()['resolved']==1


def test_event_confirm_revoke_and_new_event_boundary(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='fixture event',actor='fixture')
    source=dict(content_id='c',text='verifiable original',url='https://example.org',publisher='A',published_at='2026-09-29',reason='same',relation='same_event',facts=[{'quote':'verifiable'}])
    v=store.propose(key,source)
    assert store.rebuild(key)['source_count']==0
    one=store.decide(key,v,actor='fixture',decision='confirmed',reason='review',upstream_group='official')
    store.propose(key,source)
    assert store.event(key)['sources'][0]['state']=='confirmed'
    two=store.decide(key,v,actor='fixture',decision='rejected',reason='wrong attribution')
    assert one['source_count']==1 and two['source_count']==0
    assert (tmp_path/(key+'.'+one['summary_version']+'.md')).exists()


def test_health_no_false_recovery_or_remote_inference():
    fact=dict(machine='mac',producer='tg',kind='fetch_failure',observed_at='2026-09-29',evidence='local log',next_action='inspect',authority='local')
    a=health_report([fact]);b=health_report([fact],a)
    assert b['findings'][0]['occurrences']==2
    assert health_report([],b)['findings']==b['findings']
    assert health_report([{**fact,'status':'recovered'}],b)['recoveries']
    with pytest.raises(ValueError):health_report([{**fact,'authority':'replica'}])


def test_recall_never_infers_read_from_expand_and_limits_three():
    hits=[dict(content_id=str(i),title='x',published_at='2026-09-28T00:00:00+00:00',text='source',canonical_url='https://example.org') for i in range(5)]
    r=task_results({'hits':hits},task='recall',feedback=[{'event_type':'expand','content_id':'0'}],delivered_ids={'1','2','3','4'})
    assert r['scope']=='delivered' and len(r['results'])==3
    r=task_results({'hits':hits},task='recall',feedback=[{'event_type':'read','content_id':'0'}],delivered_ids={'1'})
    assert [h['content_id'] for h in r['results']]==['0']


def test_profile_invalid_preserves_original():
    candidates=[dict(content_id='1',text='source',source_ref='ref')]
    assert rank_candidates(candidates,{})['candidates']==candidates
    assert rank_candidates(candidates,{})['fallback']


def test_progress_links_only_confirmed_event_members():
    hits=[dict(content_id='c1',published_at='2026-09-28T00:00:00+00:00',text='source'),
          dict(content_id='c2',published_at='2026-08-01T00:00:00+00:00',text='old')]
    events=[dict(id='event1',title='confirmed event',path='/local/event.md',sources=[{'content_id':'c1','state':'confirmed'}]),
            dict(id='event2',title='unreviewed',path='/local/other.md',sources=[{'content_id':'c1','state':'proposed'}])]
    result=task_results({'hits':hits},task='progress',feedback=[],delivered_ids=set(),
                        now=datetime(2026,9,29,tzinfo=timezone.utc),event_archives=events)
    assert len(result['results'])==1
    assert result['results'][0]['event_archives']==[{'event_id':'event1','title':'confirmed event','path':'/local/event.md'}]


def test_candidate_reports_diff_and_only_verified_evaluation(tmp_path):
    from chat_daily_tg.rubric_candidates import candidate_summaries
    store,c,active,m,r=candidate_fixture(tmp_path)
    summaries=candidate_summaries(store.root)
    assert summaries[0]['added_lines'] > 0
    assert summaries[0]['evaluation_path'] is None
    assert (store.root/(c['id']+'.diff')).exists()
    store.evaluated(c['id'],m,r)
    assert candidate_summaries(store.root)[0]['evaluation_id']==r['evaluation_id']
    (store.root/(c['id']+'.evaluation.json')).write_text('{}')
    assert candidate_summaries(store.root)[0]['evaluation_path'] is None


def test_candidate_tampering_cannot_be_activated(tmp_path):
    store,c,active,m,r=candidate_fixture(tmp_path)
    store.evaluated(c['id'],m,r)
    store.review(c['id'],actor='fixture',decision='approve',reason='test',explanations={'0':'fixture'})
    path=store.root/(c['id']+'.json')
    row=json.loads(path.read_text());row['text']+='tampered';path.write_text(json.dumps(row))
    with pytest.raises(ValueError,match='identity'):
        store.activate(c['id'],active)
    assert active.read_text()==c['parent_text']


def test_daily_review_keeps_source_unchanged_and_records_missing_input(tmp_path):
    import sqlite3
    from chat_daily_tg.content_operations import daily_review
    path=tmp_path/'growth.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE growth_segments(id TEXT,sent_at TEXT,sent_style TEXT)')
        db.execute("INSERT INTO growth_segments VALUES ('card','2026-09-28T00:00:00+00:00','ambiguous')")
    before=path.read_bytes()
    request=dict(machine='fixture',sources=[
        {'kind':'growth','path':str(path),'machine':'fixture','authority':'local','target':{'chat_id':1}},
        {'kind':'replies','path':str(tmp_path/'missing.db'),'machine':'fixture','authority':'local'}],
        now='2026-09-29T00:00:00+00:00')
    a=daily_review(tmp_path/'out',**request)
    b=daily_review(tmp_path/'out',**request)
    assert a==b and a['unknown']==1 and a['source_failures']==1
    assert path.read_bytes()==before and not (tmp_path/'missing.db').exists()
    from pathlib import Path
    report=json.loads(Path(a['json']).read_text())
    assert report['delivery']['cases'][0]['age_seconds']==86400
    assert '输入读取失败' in Path(a['markdown']).read_text()
    with pytest.raises(ValueError,match='authoritative'):
        daily_review(tmp_path/'other',machine='remote',sources=request['sources'])


def test_event_suggestion_reuses_judge_and_preserves_manual_decision(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='specific event',actor='fixture')
    first=dict(content_id='one',text='first original',url='https://example.org/one',publisher='first',
               published_at='2026-09-28T00:00:00+00:00',reason='seed',relation='same_event')
    class LLM:
        model='fixture-model'
        def __init__(self):self.calls=0
        def chat(self,*a,**k):
            self.calls+=1
            return '{"same_event":true,"new_info":"substantial","reason":"new facts"}',{}
    llm=LLM()
    candidate={**first,'content_id':'two','text':'second original','url':'https://example.org/two'}
    assert store.suggest(key,candidate,llm=llm)['status']=='needs_seed_review'
    assert llm.calls==0
    version=store.propose(key,first)
    store.decide(key,version,actor='fixture',decision='confirmed',reason='seed review')
    result=store.suggest(key,candidate,llm=llm)
    assert result['status']=='proposed' and result['relation']=='same_event'
    assert llm.calls==1
    new=next(s for s in store.event(key)['sources'] if s['content_id']=='two')
    assert new['state']=='proposed'
    store.decide(key,new['version'],actor='fixture',decision='rejected',reason='different event')
    assert store.suggest(key,candidate,llm=llm)['status']=='human_decision_preserved'
    assert llm.calls==1


def test_negative_same_event_does_not_invent_same_topic(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='event',actor='fixture')
    source=dict(content_id='one',text='original',url='https://example.org',publisher='first',
                published_at='2026-09-28T00:00:00+00:00',reason='seed',relation='same_event')
    version=store.propose(key,source);store.decide(key,version,actor='fixture',decision='confirmed',reason='seed')
    class LLM:
        def chat(self,*a,**k):return '{"same_event":false,"new_info":"substantial"}',{}
    result=store.suggest(key,{**source,'content_id':'two','text':'different'},llm=LLM())
    assert result['relation']=='undetermined'
    assert len(store.event(key)['sources'])==1


def test_pending_dossier_materials_visible_without_confirming_membership(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='followed event',actor='fixture')
    initial=store.rebuild(key)
    from pathlib import Path
    snapshot=tmp_path/(key+'.'+initial['summary_version']+'.md')
    frozen=snapshot.read_text()
    store.propose(key,dict(content_id='one',text='original excerpt',url='https://example.org',
        publisher='publisher',published_at='2026-09-29',reason='pending verification',relation='same_event',
        timestamp_basis='delivery time',target_url='https://t.me/c/123/4'))
    result=store.rebuild(key)
    text=Path(result['path']).read_text()
    assert '待核实关联' in text and 'original excerpt' in text and 'delivery time' in text
    assert result['source_count']==0 and result['summary_version']==initial['summary_version']
    assert snapshot.read_text()==frozen


def test_invalid_fact_never_persists_or_confirms(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='event',actor='fixture')
    source=dict(content_id='one',text='actual text',url='https://example.org',publisher='publisher',
                published_at='2026-09-29',reason='seed',relation='same_event',facts=[{'quote':'invented'}])
    with pytest.raises(ValueError,match='source anchor'):store.propose(key,source)
    assert store.event(key)['sources']==[]
    source['facts']=[{'quote':'actual'}]
    version=store.propose(key,source)
    path=tmp_path/(key+'.event.json')
    row=json.loads(path.read_text());row['sources'][0]['text']='actual changed text'
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError,match='identity mismatch'):
        store.decide(key,version,actor='fixture',decision='confirmed',reason='reviewed')


def test_correction_retains_original_and_requires_existing_version(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='event',actor='fixture')
    original=dict(content_id='one',text='old fact',url='https://example.org',publisher='publisher',
                  published_at='2026-09-28',reason='seed',relation='same_event')
    version=store.propose(key,original)
    corrected={**original,'text':'new corrected fact','published_at':'2026-09-29','correction_of':'missing'}
    with pytest.raises(ValueError,match='correction'):store.propose(key,corrected)
    corrected['correction_of']=version
    new_version=store.propose(key,corrected)
    assert new_version!=version
    sources=store.event(key)['sources']
    assert sources[0]['text']=='old fact' and sources[1]['correction_of']==version


def test_health_notifications_merge_and_recovery_retry(tmp_path):
    from chat_daily_tg.content_operations import notify_health
    finding=dict(machine='local',producer='tg',kind='failure',observed_at='2026-09-29',
                 evidence='fixture',next_action='inspect',authority='local')
    first=health_report([finding]);calls=[]
    def warning(title,message,**kw):calls.append(('warning',kw['source_event_id']));return kw['source_event_id']
    failed=[True]
    def recovery(event_id,**kw):
        calls.append(('recovery',event_id))
        return not failed[0]
    notify_health(tmp_path,first,warning=warning,recovery=recovery)
    second=health_report([finding],first)
    assert notify_health(tmp_path,second,warning=warning,recovery=recovery)['outcomes']==[]
    restored=health_report([{**finding,'status':'recovered'}],second)
    notify_health(tmp_path,restored,warning=warning,recovery=recovery)
    failed[0]=False
    notify_health(tmp_path,{'findings':[],'recoveries':[]},warning=warning,recovery=recovery)
    assert [c[0] for c in calls]==['warning','recovery','recovery']
    assert len({c[1] for c in calls})==1
    assert notify_health(tmp_path,restored,warning=warning,recovery=recovery)['outcomes']==[]


def test_health_warning_failure_reuses_identity(tmp_path):
    from chat_daily_tg.content_operations import notify_health
    h=health_report([dict(machine='local',producer='tg',kind='failure',observed_at='2026-09-29',
                         evidence='fixture',next_action='inspect',authority='local')])
    calls=[]
    def fail(title,message,**kw):calls.append(kw['source_event_id']);return None
    for _ in range(2):notify_health(tmp_path,h,warning=fail)
    assert len(calls)==2 and calls[0]==calls[1]


def test_recall_uses_confirmed_source_identity_and_distinct_cards():
    hits=[dict(content_id='source',text='chunk 1'),dict(content_id='source',text='chunk 2'),dict(content_id='other',text='other')]
    feedback=[{'event_type':'read','content_id':'delivery','source_content_id':'source','mapping_status':'confirmed','confirmed':True}]
    result=task_results({'hits':hits},task='recall',feedback=feedback,delivered_ids={'other'})
    assert [r['content_id'] for r in result['results']]==['source']
    assert result['results'][0]['snippet']=='chunk 1'
    pending=[{'event_type':'read','content_id':None,'mapping_status':'pending','confirmed':False}]
    result=task_results({'hits':hits},task='recall',feedback=pending,delivered_ids={'other'})
    assert result['scope']=='read' and result['results']==[] and result['unresolved_read_events']==1
    expanded=task_results({'hits':hits},task='recall',feedback=pending,delivered_ids={'other'},expand_archive=True)
    assert expanded['expanded'] and len(expanded['results'])==2


def test_event_progress_distinguishes_new_source_from_new_fact(tmp_path):
    store=EventFiles(tmp_path);key=store.create(title='event',actor='fixture')
    source=dict(content_id='one',text='same fact',url='https://example.org/one',publisher='A',
        published_at='2026-09-29',reason='seed',relation='same_event',facts=[{'quote':'same fact'}])
    version=store.propose(key,source)
    first=store.decide(key,version,actor='fixture',decision='confirmed',reason='seed')
    assert first['progress_count']==1
    second=store.propose(key,{**source,'content_id':'two','url':'https://example.org/two','publisher':'B'})
    next_result=store.decide(key,second,actor='fixture',decision='confirmed',reason='repost')
    assert next_result['progress_count']==1 and next_result['source_count']==2
    assert next_result['summary_version']!=first['summary_version']
    store.set_status(key,state='paused',actor='fixture',reason='pause suggestions')
    class LLM:
        def chat(self,*a,**k):raise AssertionError('paused event must not call model')
    assert store.suggest(key,{**source,'content_id':'three'},llm=LLM())['status']=='paused'
    assert store.rebuild(key)['state']=='paused'


@pytest.mark.parametrize('mutation',['label','hash','duplicate','metrics','disagreement'])
def test_rubric_evaluation_rejects_frozen_evidence_mismatch(tmp_path,mutation):
    store,c,active,m,r=candidate_fixture(tmp_path)
    if mutation=='label':r['rows'][0]['human_label']='omit'
    elif mutation=='hash':r['rows'][0]['input_hash']='wrong'
    elif mutation=='duplicate':r['rows'].append(r['rows'][0])
    elif mutation=='metrics':r['metrics']['a']['retention']['rate']=0
    else:r['disagreements']=[]
    with pytest.raises(ValueError):store.evaluated(c['id'],m,r)
    assert store.read(c['id'])['state']=='draft'
    assert active.read_text()==c['parent_text']
