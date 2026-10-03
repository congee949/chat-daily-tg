"""Read-only reply trial metrics from durable operations, not inferred reading."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3


def feedback_report(*, root):
    path=Path(root)/'operations.sqlite3'
    if not path.is_file():
        return {'schema':'feedback-trial.v1','available':False,'reason':'operations database missing'}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('SELECT id,state,envelope,mapping,response_ids FROM operations').fetchall()
        histories=db.execute('SELECT operation_id,state,detail FROM history ORDER BY id').fetchall()
    history_by_id={};attempts=set()
    for operation,state,detail in histories:
        history_by_id.setdefault(operation,[]).append(state)
        value=json.loads(detail)
        if value.get('attempt_id'):attempts.add((operation,value['attempt_id']))
    counts={'commands':len(rows),'expand_commands':0,'filter_commands':0,'uniquely_matched':0,
            'feedback_recorded':0,'expand_succeeded':0,'reply_unknown':0,'needs_match':0,
            'failed':0,'response_absent':0,'retry_approved':0,'send_attempts':len(attempts)}
    cases=[]
    for operation,state,envelope,mapping,response_ids in rows:
        message=json.loads(envelope)['message'];command=message['text'].strip()
        expanded=command=='展开'
        counts['expand_commands']+=expanded
        counts['filter_commands']+=command=='少推这类'
        matched=mapping is not None
        counts['uniquely_matched']+=matched
        recorded='recorded' in history_by_id.get(operation,[])
        counts['feedback_recorded']+=recorded
        ids=json.loads(response_ids) if response_ids else []
        confirmed=bool(ids) and all(type(i) is int and i>0 for i in ids)
        counts['expand_succeeded']+=expanded and state=='responded' and confirmed
        if state=='response_unknown':counts['reply_unknown']+=1
        for name in ('needs_match','failed','response_absent','retry_approved'):
            counts[name]+=state==name
        cases.append({'operation_id':operation,'state':state,'command':command,'matched':matched,
                      'recorded':recorded,'reply_receipt_ids':ids if confirmed else [],
                      'content_id':json.loads(mapping).get('content_id') if mapping else None})
    return {'schema':'feedback-trial.v1','available':True,'counts':counts,'cases':cases,
            'match_rate':counts['uniquely_matched']/counts['commands'] if counts['commands'] else None,
            'expand_success_rate':counts['expand_succeeded']/counts['expand_commands'] if counts['expand_commands'] else None,
            'read_metric':None,'read_metric_reason':'reply receipts and expand requests are not read events',
            'scope':'accepted owner commands only; rejected updates are not counted'}
