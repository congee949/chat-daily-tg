"""Evidence-preserving health findings and manual unknown-delivery review."""
from __future__ import annotations
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.logging_setup import redact
from chat_daily_tg.rubric_candidates import atomic_json


class DeliveryReview:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/'review.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY,case_id TEXT NOT NULL,payload TEXT NOT NULL)''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=1)
        try:
            with db: yield db
        finally: db.close()

    def add(self,*,content_id,attempt_id,machine,producer,requested_at,target,error_type,known_receipts=None):
        if not all((content_id,attempt_id,machine,producer,requested_at,target,error_type)):
            raise ValueError('complete attempt evidence required')
        datetime.fromisoformat(requested_at.replace('Z','+00:00'))
        key=digest([machine,producer,content_id,attempt_id])
        row=dict(id=key,content_id=content_id,attempt_id=attempt_id,machine=machine,producer=producer,
                 requested_at=requested_at,target=target,error_type=redact(error_type),
                 known_receipts=known_receipts or [],state='unknown')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO cases VALUES (?,?,?)',(key,'unknown',json.dumps(row,ensure_ascii=False)))
        return key

    def decide(self,key,*,decision,actor,evidence):
        if decision not in {'confirmed_sent','confirmed_absent','retry_requested'} or not actor or not evidence:
            raise ValueError('explicit reviewer and evidence required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            value=db.execute('SELECT state,payload FROM cases WHERE id=?',(key,)).fetchone()
            if not value:raise ValueError('unknown case')
            state,row=value[0],json.loads(value[1])
            allowed={'unknown':{'confirmed_sent','confirmed_absent'},'confirmed_absent':{'retry_requested'}}
            if decision not in allowed.get(state,set()):raise ValueError('invalid review transition')
            if decision=='confirmed_sent' and not evidence.get('telegram_message_url'):
                raise ValueError('Telegram message receipt required')
            if decision=='confirmed_absent' and not evidence.get('checked_scope'):
                raise ValueError('checked chat/time/message scope required')
            transition=dict(from_state=state,state=decision,actor=actor,evidence=evidence,
                            at=datetime.now(timezone.utc).isoformat())
            row.update(state=decision,review=transition)
            db.execute('UPDATE cases SET state=?,payload=? WHERE id=?',(decision,json.dumps(row,ensure_ascii=False),key))
            db.execute('INSERT INTO history(case_id,payload) VALUES (?,?)',(key,json.dumps(transition,ensure_ascii=False)))
        return row

    def report(self,now=None):
        now=now or datetime.now(timezone.utc)
        with self.connect() as db: rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM cases ORDER BY id')]
        for row in rows:
            requested=datetime.fromisoformat(row['requested_at'].replace('Z','+00:00'))
            if requested.tzinfo is None:requested=requested.replace(tzinfo=timezone.utc)
            row['age_seconds']=max(0,(now-requested).total_seconds())
        return {'schema':'delivery-review.v1','cases':rows,
                'unknown':sum(r['state']=='unknown' for r in rows),
                'resolved':sum(r['state']!='unknown' for r in rows),
                'duplicate_sends':None,'duplicate_sends_reason':'requires receipt comparison'}

    def _project_review(self, key, decision, actor, evidence):
        with self.connect() as db:
            row=db.execute('SELECT state FROM cases WHERE id=?',(key,)).fetchone()
        if row and row[0]==decision:return
        # Later local state is preserved when replaying earlier upstream decisions.
        if row and row[0]=='retry_requested' and decision=='confirmed_absent':return
        evidence=dict(evidence)
        if evidence.get('telegram_reference'):
            evidence['telegram_message_url']=evidence['telegram_reference']
        self.decide(key,decision=decision,actor=actor,evidence=evidence)

    def import_growth(self,path,*,machine,target):
        """Import immutable ambiguous attempts and authoritative review history."""
        from contextlib import closing
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute("SELECT id,sent_at FROM growth_segments WHERE sent_style='ambiguous'").fetchall()
            reviews=[]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='growth_delivery_reviews'").fetchone():
                reviews=db.execute('SELECT segment_id,attempt_sent_at,decision,actor,evidence FROM growth_delivery_reviews ORDER BY id').fetchall()
        result=[]
        for seg_id,sent_at in dict.fromkeys([*rows,*[(r[0],r[1]) for r in reviews]]):
            key=self.add(content_id=seg_id,attempt_id='growth:'+seg_id+':'+sent_at,machine=machine,
                         producer='growth',requested_at=sent_at,target={
                             'configured_target_hint':target,'mapping_status':'needs_historical_verification',
                             'reason':'growth segment does not store the actual delivery target'},error_type='ambiguous')
            result.append(key)
            for rid,when,decision,actor,evidence in reviews:
                if rid==seg_id and when==sent_at:
                    self._project_review(key,decision,actor,json.loads(evidence))
        return result

    def import_replies(self,path,*,machine):
        from contextlib import closing
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT id,envelope,mapping,updated,state,response_ids FROM operations').fetchall()
            histories={key:db.execute('SELECT state,detail,occurred_at FROM history WHERE operation_id=? ORDER BY id',(key,)).fetchall()
                       for key,*_ in rows}
        result=[]
        for operation,envelope,mapping,updated,state,response_ids in rows:
            message=json.loads(envelope)['message'];mapping=json.loads(mapping or '{}')
            attempt=None;case=None
            for phase,detail,when in histories[operation]:
                entry=json.loads(detail)
                if phase=='response_unknown' and entry.get('attempt_id'):
                    attempt=entry['attempt_id']
                    case=self.add(content_id=mapping.get('content_id',operation),attempt_id=attempt,machine=machine,
                        producer='content-feedback',requested_at=when,target={'chat_id':message['chat']['id'],
                        'operation_id':operation,'thread_id':message.get('message_thread_id')},error_type='response_unknown')
                    result.append(case)
                if case and entry.get('decision') in {'confirmed_sent','confirmed_absent','retry_requested'}:
                    self._project_review(case,entry['decision'],entry['actor'],entry['evidence'])
            if case and state=='responded' and response_ids:
                ids=json.loads(response_ids)
                if isinstance(ids,list) and ids and all(type(mid) is int and mid>0 for mid in ids):
                    self._project_review(case,'confirmed_sent','sender_receipt',{
                        'chat_id':message['chat']['id'],'message_ids':ids,
                        'telegram_reference':f"chat={message['chat']['id']}; messages={ids}"})
            if attempt is None and state=='response_unknown':
                result.append(self.add(content_id=mapping.get('content_id',operation),attempt_id=operation,machine=machine,
                    producer='content-feedback',requested_at=updated,target={'chat_id':message['chat']['id'],
                    'operation_id':operation,'thread_id':message.get('message_thread_id')},error_type='response_unknown'))
        return list(dict.fromkeys(result))


def health_report(observations,previous=None):
    """Input facts must name their machine, measurement kind and direct evidence.

    Generic source timing logs never qualify as successful acquisition. Unknown
    freshness stays explicit when a source has no success timestamp.
    """
    previous=previous or {'findings':[]}
    prior={r['key']:r for r in previous['findings']}
    current={};recoveries=[]
    for obs in observations:
        if any(not obs.get(k) for k in ('machine','producer','kind','observed_at','evidence','next_action')):
            raise ValueError('machine-local health evidence required')
        if obs.get('authority')!='local':raise ValueError('replica cannot establish remote health')
        key=digest([obs['machine'],obs['producer'],obs['kind'],obs.get('source_ref')])
        old=prior.get(key)
        if obs.get('status')=='recovered':
            if old:recoveries.append({**obs,'key':key,'previous':old})
            continue
        if obs.get('kind')=='duration' and obs.get('last_fetch_success'):
            raise ValueError('duration is not fetch success')
        current[key]={**obs,'key':key,'first_seen':old['first_seen'] if old else obs['observed_at'],
                      'last_seen':obs['observed_at'],'occurrences':old['occurrences']+1 if old else 1,
                      'last_fetch_success':obs.get('last_fetch_success'),
                      'latest_content_at':obs.get('latest_content_at')}
    # Missing observations are not recovery evidence.
    for key,old in prior.items():
        if key not in current and not any(r['key']==key for r in recoveries):current[key]=old
    return {'schema':'content-health.v1','findings':list(current.values()),'recoveries':recoveries}


def daily_review(root, *, machine, sources, observations=None, now=None):
    """Build local review artifacts from explicitly named authoritative inputs.

    Each source failure is retained separately; a missing input never becomes a
    healthy result. This command only mutates its own review/output directory.
    """
    from zoneinfo import ZoneInfo
    from chat_daily_tg.rubric_candidates import atomic_text
    if not machine or not isinstance(sources, list):
        raise ValueError('machine and explicit source list required')
    now = now or datetime.now(timezone.utc)
    if isinstance(now, str):now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    if now.tzinfo is None:raise ValueError('timezone-aware report time required')
    root = Path(root)
    store = DeliveryReview(root / 'state')
    imported = []; failures = []
    for source in sources:
        if source.get('machine') != machine or source.get('authority') != 'local':
            raise ValueError('source must be authoritative on the declared execution machine')
        kind = source.get('kind')
        if kind not in {'growth', 'replies'}:
            raise ValueError('unsupported review source')
        path = Path(source['path']).expanduser()
        try:
            if kind == 'growth':
                ids = store.import_growth(path, machine=machine, target=source['target'])
            else:
                ids = store.import_replies(path, machine=machine)
            imported.append({'kind':kind,'path':str(path),'case_ids':ids})
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            failures.append({'kind':kind,'path':str(path),'error_type':type(exc).__name__,
                             'next_action':'核对源文件及表结构，再重新导入'})
    previous_path = root / 'health-latest.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    health = health_report(observations or [], previous)
    report = {'schema':'content-daily-review.v1','machine':machine,'generated_at':now.isoformat(),
              'imports':imported,'source_failures':failures,'health':health,
              'delivery':store.report(now=now)}
    day = now.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    output = root / day
    output.mkdir(parents=True, exist_ok=True)
    identity = digest(report)
    json_path = output / (identity + '.json')
    md_path = output / (identity + '.md')
    lines = [f'# 投递复核 · {day}', '', f'执行机器：{machine}',
             f'生成时间：{now.isoformat()}', '',
             f"未知投递 {report['delivery']['unknown']} 条；已复核 {report['delivery']['resolved']} 条；输入失败 {len(failures)} 项。", '']
    for case in report['delivery']['cases']:
        lines.extend([f"## {case['producer']} · {case['content_id']}", '',
                      f"状态：{case['state']}；年龄：{round(case['age_seconds']/3600, 1)} 小时。",
                      f"复核 ID：`{case['id']}`", f"请求时间：{case['requested_at']}",
                      '目标：' + json.dumps(case['target'], ensure_ascii=False),
                      '已知回执：' + json.dumps(case['known_receipts'], ensure_ascii=False),
                      '错误分类：' + case['error_type'],
                      '后续操作：核对 Telegram 消息，记录实际消息引用或未送达核对范围。', ''])
    for failure in failures:
        lines.extend(['## 输入读取失败', '', f"{failure['kind']}：{failure['path']}",
                      failure['error_type'], failure['next_action'], ''])
    for finding in health['findings']:
        lines.extend([f"## 健康发现 · {finding['producer']} · {finding['kind']}", '',
                      f"首次：{finding['first_seen']}；最近：{finding['last_seen']}",
                      f"成功抓取时间：{finding['last_fetch_success'] or '未知'}；最新内容时间：{finding['latest_content_at'] or '未知'}",
                      '证据：' + str(finding['evidence']), '后续操作：' + finding['next_action'], ''])
    for recovery in health['recoveries']:
        lines.extend([f"## 恢复 · {recovery['producer']} · {recovery['kind']}", '',
                      '证据：' + str(recovery['evidence']), ''])
    atomic_json(json_path, report)
    atomic_text(md_path, '\n'.join(lines))
    atomic_json(previous_path, health)
    atomic_json(root / 'latest.json', {'json':str(json_path),'markdown':str(md_path),'report_hash':identity})
    return {'json':str(json_path),'markdown':str(md_path),'report_hash':identity,
            'unknown':report['delivery']['unknown'],'source_failures':len(failures)}


def notify_health(root, health, *, warning=None, recovery=None):
    """Idempotent incident bridge; durable recovery retries survive later reports."""
    import fcntl
    from chat_daily_tg.incident_client import report_warning, report_recovery
    warning = warning or report_warning
    recovery = recovery or report_recovery
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/'health-notifications.json'
    with (root/'.health-notifications.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads(path.read_text()) if path.exists() else {'episodes':{}}
        episodes=state['episodes']
        for finding in health.get('findings',[]):
            episode_id=digest([finding['key'],finding['first_seen']])
            if episode_id not in episodes:
                episodes[episode_id]={'event_id':'content-health:'+episode_id,'finding':finding,
                                      'warning_accepted':False,'recovery':None,'recovery_accepted':False}
            else:
                episodes[episode_id]['finding']=finding
        for restored in health.get('recoveries',[]):
            prior=restored['previous']
            episode_id=digest([restored['key'],prior['first_seen']])
            if episode_id in episodes:
                episodes[episode_id]['recovery']=restored
        # Persist IDs before any outbound call; a retry uses the same controller identity.
        atomic_json(path,state)
        outcomes=[]
        for episode_id,episode in episodes.items():
            finding=episode['finding']
            title=f"内容链路 · {finding['machine']} · {finding['producer']} · {finding['kind']}"
            try:
                if not episode['warning_accepted']:
                    if episode['recovery'] is not None:
                        outcomes.append({'episode':episode_id,'status':'recovered_before_notification'})
                        continue
                    message=f"首次：{finding['first_seen']}\n最近：{finding['last_seen']}\n影响：{finding.get('impact','待核实')}\n后续操作：{finding['next_action']}"
                    accepted=warning(title,message,source_event_id=episode['event_id'],
                        metadata={'machine':finding['machine'],'producer':finding['producer'],
                                  'evidence':finding['evidence'],'occurrences':finding['occurrences']})
                    if accepted==episode['event_id']:
                        episode['warning_accepted']=True
                    outcomes.append({'episode':episode_id,'status':'warning_accepted' if accepted==episode['event_id'] else 'warning_pending'})
                if episode['warning_accepted'] and episode['recovery'] is not None and not episode['recovery_accepted']:
                    restored=episode['recovery']
                    accepted=recovery(episode['event_id'],title=title+' · 已恢复',
                        message='恢复证据：'+str(restored['evidence']),
                        metadata={'machine':finding['machine'],'producer':finding['producer']})
                    if accepted is True:episode['recovery_accepted']=True
                    outcomes.append({'episode':episode_id,'status':'recovery_accepted' if accepted is True else 'recovery_pending'})
            except Exception as exc:
                outcomes.append({'episode':episode_id,'status':'pending','error_type':type(exc).__name__})
            atomic_json(path,state)
        return {'schema':'content-health-notify.v1','outcomes':outcomes}
