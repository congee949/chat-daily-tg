"""Read-only machine-local health observations from authoritative runtime records."""
from __future__ import annotations
from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3


class HealthInputError(ValueError):
    def __init__(self, code, **evidence):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


def timestamp(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('health timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def collect_health(*, machine, growth_db=None, jev_journal=None, now=None, journal_max_age_seconds=86400):
    """Missing/stale data never creates a recovery. No schema initialization or network."""
    if machine!=socket.gethostname():raise ValueError('machine must match the execution host')
    now=timestamp(now) if now is not None else datetime.now(timezone.utc)
    if journal_max_age_seconds<=0:raise ValueError('positive freshness interval required')
    observations=[];reads=[]
    def emit(producer,kind,evidence,next_action,**fields):
        observations.append({'machine':machine,'producer':producer,'kind':kind,'authority':'local',
            'observed_at':now.isoformat(),'evidence':evidence,'next_action':next_action,**fields})
    if growth_db is not None:
        path=Path(growth_db).expanduser().resolve()
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                rows=db.execute("SELECT id,claim_until FROM growth_segments WHERE status='sending'").fetchall()
            expired=[]
            for identity,deadline in rows:
                if deadline is None or timestamp(deadline)<now:expired.append(identity)
            evidence={'path':str(path),'sending_count':len(rows),'expired_claim_ids':expired}
            emit('growth','expired_claim',evidence,
                 '核对守护任务日志与 claim 恢复；不要仅据租约过期手动重发',
                 status='active' if expired else 'recovered',impact=f'{len(expired)} 条 sending 租约过期或缺少截止时间')
            emit('growth','health_input',{'path':str(path),'read':'succeeded'},'本机源库读取正常',status='recovered')
            reads.append({'producer':'growth','path':str(path),'status':'read'})
        except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
            emit('growth','health_input',{'path':str(path),'error_type':type(exc).__name__},
                 '核对本机源库、表结构及时间字段',status='active',impact='队列健康未知')
    if jev_journal is not None:
        path=Path(jev_journal).expanduser().resolve()
        try:
            latest=None;latest_time=None;count=0
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():continue
                    row=json.loads(line)
                    if row.get('schema')!='chatdaily.jev-dedup-judge.v1':continue
                    when=timestamp(row['timestamp']);count+=1
                    if latest_time is None or when>latest_time:latest,latest_time=row,when
            if latest is None:raise HealthInputError('no_judge_receipt')
            age=(now-latest_time).total_seconds()
            if age<0:
                raise HealthInputError('future_judge_receipt',latest_receipt_at=latest_time.isoformat(),age_seconds=age)
            if age>journal_max_age_seconds:
                raise HealthInputError('stale_judge_receipt',latest_receipt_at=latest_time.isoformat(),
                                       age_seconds=age,max_age_seconds=journal_max_age_seconds,
                                       latest_recorded_status=latest.get('status'))
            if latest.get('status') not in {'ok','error','uncertain'} or type(latest.get('fallback_used')) is not bool:
                raise HealthInputError('incomplete_judge_receipt')
            degraded=latest['fallback_used'] or latest['status']!='ok'
            emit('jev','model_degradation',{'path':str(path),'timestamp':latest['timestamp'],
                'status':latest['status'],'fallback_used':latest['fallback_used']},
                '核对该次 Jev 错误与 fallback 回执',status='active' if degraded else 'recovered',
                impact='最新裁判调用降级' if degraded else '最新裁判调用正常')
            emit('jev','health_input',{'path':str(path),'read':'succeeded'},'本机裁判记录读取正常',status='recovered')
            reads.append({'producer':'jev','path':str(path),'status':'read','records':count})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            emit('jev','health_input',{'path':str(path),'error_type':type(exc).__name__,
                 'reason_code':getattr(exc,'code','unreadable_judge_journal'),
                 **getattr(exc,'evidence',{})},
                 '核对日志新鲜度、完整性与任务是否按计划运行',status='active',impact='裁判近期健康未知')
    return {'schema':'content-health-observations.v1','machine':machine,'observations':observations,'reads':reads}


def collect_fetch_health(*, machine, journals, empty_threshold=3):
    """Aggregate explicit acquisition receipts, never infer freshness from log mtimes."""
    if machine!=socket.gethostname():raise ValueError('machine must match the execution host')
    if type(empty_threshold) is not int or empty_threshold<1:raise ValueError('positive empty threshold required')
    groups={};failures=[];seen=set()
    for filename in journals:
        path=Path(filename)
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():continue
                    row=json.loads(line)
                    if row.get('schema')!='source-fetch.v1' or row.get('machine')!=machine:
                        raise ValueError('journal is not local acquisition evidence')
                    timestamp(row['finished_at'])
                    if row['status'] not in {'success','no_update','parsed_empty','failed','sync_completed'}:
                        raise ValueError('invalid receipt status')
                    if row['status'] not in {'failed','sync_completed'} and (type(row.get('source_count')) is not int or row['source_count']<0):
                        raise ValueError('missing authoritative count')
                    if row['attempt_id'] in seen:continue
                    seen.add(row['attempt_id'])
                    groups.setdefault((row['producer'],row['source_ref']),[]).append({**row,'journal':str(path)})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            failures.append({'path':str(path),'error_type':type(exc).__name__})
    observations=[]
    for (producer,source),rows in groups.items():
        rows.sort(key=lambda r:timestamp(r['finished_at']))
        latest=rows[-1];empty=0
        for row in reversed(rows):
            if row['status']=='no_update':empty+=1
            else:break
        successful=[r for r in rows if r['status']!='failed']
        base={'machine':machine,'producer':producer,'source_ref':source,'authority':'local',
              'observed_at':latest['finished_at'],'last_fetch_success':successful[-1]['finished_at'] if successful else None,
              'latest_content_at':latest.get('latest_content_at'),
              'evidence':{'journal':latest['journal'],'attempt_id':latest['attempt_id'],
                          'status':latest['status'],'source_count':latest.get('source_count'),
                          'count_basis':latest.get('count_basis','source_rows'),'consecutive_empty':empty}}
        for kind,active,action in [
            ('fetch_failure',latest['status']=='failed','核对该信源导出错误与权限'),
            ('parsed_empty',latest['status']=='parsed_empty','核对有源消息但清理后无正文的解析结果'),
            ('consecutive_empty',empty>=empty_threshold,'核对信源是否确实无更新；零结果本身不表示抓取失败')]:
            observations.append({**base,'kind':kind,'status':'active' if active else 'recovered','next_action':action})
    # A partial journal read is not sufficient to close prior incidents.
    if failures:
        observations=[o for o in observations if o['status']!='recovered']
    return {'schema':'source-fetch-health.v1','observations':observations,'input_failures':failures}
