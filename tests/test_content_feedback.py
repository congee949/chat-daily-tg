import json
from chat_daily_tg.content_feedback import ReplyIntake
from chat_daily_tg.intent_feedback import FeedbackStore
from chat_daily_tg.sent_content_ledger import append_message_ids


def setup(tmp_path):
    ledger=tmp_path/'ledger.jsonl'
    append_message_ids([10,11],chat_id=-100,thread_id=2,producer='chatdaily_raw',
        source_kind='telegram_channel',source_ref='https://t.me/public/5',source_message_ids=[5,6],
        url='https://t.me/public/5',content='processed card',path=ledger,
        source_messages=[{'message_id':5,'text':'original'},{'message_id':6,'text':''}])
    intake=ReplyIntake(tmp_path/'ops',bot_id=1,owner_id=2,targets=[(-100,2)],text_ledger=ledger)
    update={'update_id':1,'message':{'chat':{'id':-100},'from':{'id':2},'message_thread_id':2,
                                   'reply_to_message':{'message_id':11},'text':'展开'}}
    return intake,update


def test_idempotency_album_and_restart(tmp_path):
    i,u=setup(tmp_path);assert i.accept(u) and i.accept(u)
    calls=[]
    def send(m,t):calls.append(t);return [40]
    assert i.drain(send)=={'1:1':'responded'}
    assert i.drain(send)=={}
    assert len(calls)==1 and 'original' in calls[0]
    assert len(list(FeedbackStore(i.root/'feedback').iter_events()))==1
    u['update_id']=2;u['message']['reply_to_message']['message_id']=10
    assert i.resolve(u['message'])['content_id']==i.resolve({**u['message'],'reply_to_message':{'message_id':11}})['content_id']


def test_reject_owner_target_and_needs_match(tmp_path):
    i,u=setup(tmp_path);u['message']['from']['id']=3
    assert not i.accept(u)
    u['message']['from']['id']=2;u['message']['message_thread_id']=3
    assert not i.accept(u)
    u['message']['message_thread_id']=2;u['message']['reply_to_message']['message_id']=99
    assert i.accept(u)
    assert i.drain(lambda m,t: (_ for _ in ()).throw(AssertionError()))=={'1:1':'needs_match'}


def test_unknown_never_resends_and_history_survives(tmp_path):
    i,u=setup(tmp_path);i.accept(u)
    def fail(m,t):raise TimeoutError('do not log body')
    assert i.drain(fail)=={'1:1':'response_unknown'}
    assert i.drain(lambda m,t:[55])=={}
    with i.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM history').fetchone()[0]>=5
    assert len(list(FeedbackStore(i.root/'feedback').iter_events()))==1


def test_legacy_or_tampered_text_never_expands_as_original(tmp_path):
    i,u=setup(tmp_path)
    rows=[json.loads(line) for line in i.text_ledger.read_text().splitlines()]
    for row in rows:row.pop('original_messages')
    i.text_ledger.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    assert i.resolve(u['message']) is None
    for row in rows:row['original_messages']=[{'message_id':5,'text':'tampered'},{'message_id':6,'text':''}]
    i.text_ledger.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    assert i.resolve(u['message']) is None


def test_absence_needs_explicit_retry_and_keeps_feedback_idempotent(tmp_path):
    import pytest
    i,u=setup(tmp_path);i.accept(u)
    def fail(m,t):raise TimeoutError()
    i.drain(fail)
    with pytest.raises(ValueError):i.review_response('1:1',decision='retry_requested',actor='fixture',evidence={'chat_id':-100,'reason':'retry'})
    i.review_response('1:1',decision='confirmed_absent',actor='fixture',evidence={'chat_id':-100,'checked_scope':'fixture time window'})
    assert i.drain(lambda m,t:[99])=={}
    i.review_response('1:1',decision='retry_requested',actor='fixture',evidence={'chat_id':-100,'reason':'explicit retry'})
    assert i.drain(lambda m,t:[99])=={'1:1':'responded'}
    assert len(list(FeedbackStore(i.root/'feedback').iter_events()))==1
    with i.connect() as db:
        attempts=[json.loads(r[0])['attempt_id'] for r in db.execute("SELECT detail FROM history WHERE state='response_unknown'") if 'attempt_id' in json.loads(r[0])]
    assert len(attempts)==2 and len(set(attempts))==2


def test_confirm_sent_does_not_resend_and_checks_target(tmp_path):
    import pytest
    i,u=setup(tmp_path);i.accept(u)
    i.drain(lambda m,t:None)
    evidence={'chat_id':-999,'message_ids':[50],'telegram_reference':'fixture message'}
    with pytest.raises(ValueError,match='target'):i.review_response('1:1',decision='confirmed_sent',actor='fixture',evidence=evidence)
    evidence['chat_id']=-100
    i.review_response('1:1',decision='confirmed_sent',actor='fixture',evidence=evidence)
    assert i.drain(lambda m,t:[55])=={}


def test_review_summary_tracks_old_attempt_and_successful_retry(tmp_path):
    from chat_daily_tg.content_operations import DeliveryReview
    i,u=setup(tmp_path);i.accept(u)
    i.drain(lambda m,t:None)
    review=DeliveryReview(tmp_path/'review')
    review.import_replies(i.db_path,machine='fixture')
    assert review.report()['unknown']==1
    i.review_response('1:1',decision='confirmed_absent',actor='fixture',evidence={'chat_id':-100,'checked_scope':'fixture window'})
    i.review_response('1:1',decision='retry_requested',actor='fixture',evidence={'chat_id':-100,'reason':'explicit retry'})
    i.drain(lambda m,t:[99])
    for _ in range(2):review.import_replies(i.db_path,machine='fixture')
    report=review.report()
    assert report['unknown']==0 and report['resolved']==2
    assert {r['state'] for r in report['cases']}=={'retry_requested','confirmed_sent'}


def test_trial_counts_logical_commands_not_retries_or_reads(tmp_path):
    from chat_daily_tg.feedback_report import feedback_report
    i,u=setup(tmp_path);i.accept(u);i.accept(u)
    i.drain(lambda m,t:[40])
    u['update_id']=2;u['message']['text']='少推这类';i.accept(u)
    i.drain(lambda m,t:None)
    u['update_id']=3;u['message']['text']='展开';u['message']['reply_to_message']['message_id']=999;i.accept(u)
    i.drain(lambda m,t:[41])
    before=i.db_path.read_bytes()
    result=feedback_report(root=i.root)
    c=result['counts']
    assert c['commands']==3 and c['uniquely_matched']==2 and c['feedback_recorded']==2
    assert c['expand_commands']==2 and c['expand_succeeded']==1 and c['send_attempts']==2
    assert c['needs_match']==1 and c['reply_unknown']==1
    assert result['read_metric'] is None and result['expand_success_rate']==.5
    assert i.db_path.read_bytes()==before
    assert feedback_report(root=tmp_path/'missing')['available'] is False


def test_public_original_with_external_button_is_matchable(tmp_path):
    i,u=setup(tmp_path)
    rows=[json.loads(line) for line in i.text_ledger.read_text().splitlines()]
    for row in rows:row['url']='https://github.com/example/project'
    i.text_ledger.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert i.resolve(u['message'])['url']=='https://github.com/example/project'
    for row in rows:row['source_ref']='https://t.me/c/123/5'
    i.text_ledger.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    assert i.resolve(u['message']) is None
