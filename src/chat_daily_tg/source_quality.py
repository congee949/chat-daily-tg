"""Windowed source review; acquisition and human usefulness stay independent."""
from collections import Counter
from datetime import datetime
from pathlib import Path
import json

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.rubric_candidates import atomic_text


def _time(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('timezone-aware source review window required')
    return dt


def weekly_quality(*, journals, samples, start, end, markdown_path=None):
    start_time,end_time=_time(start),_time(end)
    if start_time>=end_time:raise ValueError('empty source review window')
    attempts={};sample_ids={};failures=[]
    for filename in journals:
        path=Path(filename)
        try:
            with path.open(encoding='utf-8') as stream:
                for n,line in enumerate(stream,1):
                    if not line.strip():continue
                    row=json.loads(line)
                    if row.get('schema')!='source-fetch.v1':raise ValueError('unexpected journal schema')
                    if not start_time<=_time(row['finished_at'])<end_time:continue
                    if row['status'] not in {'success','no_update','sync_completed','parsed_empty','failed'}:
                        raise ValueError('invalid acquisition status')
                    key=(row['machine'],row['attempt_id'])
                    if key in attempts and attempts[key]!=row:raise ValueError('conflicting attempt identity')
                    attempts[key]=row
        except (OSError,ValueError,KeyError,TypeError) as exc:
            failures.append({'path':str(path),'error_type':type(exc).__name__})
    for sample in samples:
        if not start_time<=_time(sample['collected_at'])<end_time:continue
        key=sample['sample_id']
        if key in sample_ids and sample_ids[key]!=sample:raise ValueError('conflicting sample identity')
        if sample.get('human_label') is not None and (sample['human_label'] not in {'worth_sending','omit','undecided'} or not sample.get('annotator') or not sample.get('label_reason')):
            raise ValueError('human labels require attribution and reason')
        sample_ids[key]=sample
    keys={(r['producer'],r['source_ref']) for r in attempts.values()}
    keys|={(s.get('producer',s['source']),s['source_ref']) for s in sample_ids.values()}
    reports=[]
    for producer,source_ref in sorted(keys):
        fetches=[r for r in attempts.values() if (r['producer'],r['source_ref'])==(producer,source_ref)]
        items=[s for s in sample_ids.values() if (s.get('producer',s['source']),s['source_ref'])==(producer,source_ref)]
        states=Counter(r['status'] for r in fetches)
        successful=sum(states[s] for s in ('success','no_update','sync_completed'))
        labelled=[s for s in items if s.get('human_label') in {'worth_sending','omit'}]
        useful=sum(s['human_label']=='worth_sending' for s in labelled)
        known={s['upstream_group'] for s in items if s.get('upstream_confirmed') is True and s.get('upstream_group')}
        reports.append({'producer':producer,'source_ref':source_ref,'acquisition':{
            'attempts':len(fetches),'successful':successful,'statuses':dict(states),
            'success_rate':successful/len(fetches) if fetches else None},
            'usefulness':{'labelled':len(labelled),'worth_sending':useful,
                'undecided':sum(s.get('human_label')=='undecided' for s in items),
                'unlabelled':sum(s.get('human_label') is None for s in items),
                'rate':useful/len(labelled) if labelled else None},
            'provenance':{'confirmed_origin_groups':sorted(known),'confirmed_origins':len(known),
                'unknown_items':sum(not (s.get('upstream_confirmed') is True and s.get('upstream_group')) for s in items)}})
    report={'schema':'source-quality-weekly.v1','start':start,'end':end,'sources':reports,
            'input_failures':failures,'partial':bool(failures),
            'input_hash':digest({'attempts':list(attempts.values()),'samples':list(sample_ids.values())})}
    if markdown_path:
        lines=['# 来源质量复盘','',f'窗口：{start} 至 {end}（不含结束时刻）','',
               '采集、人工有用率和出处分别统计；未标注不计为无用。','']
        if failures:lines.extend([f'输入读取失败 {len(failures)} 项，以下统计为部分数据。',''])
        for row in reports:
            a,u,p=row['acquisition'],row['usefulness'],row['provenance']
            lines.extend([f"## {row['producer']} · {row['source_ref']}",'',
                f"采集成功：{a['successful']}/{a['attempts']}；状态：{json.dumps(a['statuses'],ensure_ascii=False)}。",
                f"人工有用：{u['worth_sending']}/{u['labelled']}；未标注 {u['unlabelled']}；暂无法判断 {u['undecided']}。",
                f"已确认出处组：{p['confirmed_origins']}；出处未知条目：{p['unknown_items']}。",''])
        atomic_text(markdown_path,'\n'.join(lines))
    return report
