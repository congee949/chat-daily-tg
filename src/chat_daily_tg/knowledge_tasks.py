"""Task presentation over the gated knowledge CLI, never bypassing release checks."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone


def read_scope(feedback):
    read_events=[r for r in feedback if r.get('event_type')=='read']
    read_ids=set()
    unresolved_reads=0
    for row in read_events:
        if row.get('mapping_status')=='pending' or row.get('confirmed') is False:
            unresolved_reads+=1
            continue
        identity=row.get('content_id')
        source_identity=row.get('source_content_id')
        if row.get('mapping_status')=='confirmed' and row.get('confirmed') is True and source_identity:
            identity=source_identity
        if identity:read_ids.add(identity)
        else:unresolved_reads+=1
    return read_ids,len(read_events),unresolved_reads


def task_results(result,*,task,feedback,delivered_ids,now=None,expand_archive=False,event_archives=()):
    if task not in {'recall','progress'}:raise ValueError('unknown knowledge task')
    now=now or datetime.now(timezone.utc)
    read_ids,read_event_count,unresolved_reads=read_scope(feedback)
    hits=result.get('hits',result.get('results',[]))
    if not isinstance(hits,list):raise ValueError('invalid knowledge result')
    scope='read' if read_event_count else 'delivered'
    eligible=read_ids if read_event_count else set(delivered_ids)
    if task=='progress':scope='last_seven_days'
    def recent(hit):
        try:
            dt=datetime.fromisoformat(hit['published_at'].replace('Z','+00:00'))
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            return now-timedelta(days=7)<=dt<=now
        except (ValueError,KeyError,TypeError):return False
    selected=[h for h in hits if (recent(h) if task=='progress' else h.get('content_id') in eligible)]
    expanded=False
    if not selected and expand_archive and not result.get('expansion_error'):
        selected=[h for h in hits if recent(h)] if task=='progress' else hits
        expanded=True;scope='available_archive_last_seven_days' if task=='progress' else 'available_archive'
    unique=[];seen=set()
    for hit in selected:
        identity=hit.get('content_id')
        if not identity or identity in seen:continue
        seen.add(identity);unique.append(hit)
    selected=unique
    cards=[{'content_id':h['content_id'],'title':h.get('title'),'date':h.get('published_at'),
            'url':h.get('canonical_url'),'snippet':h.get('text','')[:400],
            'source_ref':h.get('source_ref'),'scope':scope} for h in selected[:3]]
    for card in cards:
        card['event_archives'] = [
            {'event_id':event['id'],'title':event['title'],'path':event['path']}
            for event in event_archives
            if any(source.get('state')=='confirmed' and source.get('content_id')==card['content_id']
                   for source in event.get('sources',[]))
        ]
    return {'schema':'knowledge-task.v1' ,'task':task,'scope':scope,'expanded':expanded,'results':cards,
            'diagnostic':result.get('diagnostic',False),
            'candidate_count':len(hits),'scope_filter':result.get('scope_filter','applied to retrieved candidate pool'),
            'read_evidence':'explicit read events only','unresolved_read_events':unresolved_reads,
            'elapsed_ms':result.get('task_elapsed_ms',result.get('elapsed_ms',result.get('timing_ms',{}).get('total'))),
            'generation_id':result.get('generation_id'),'degraded':result.get('degraded'),
            'degraded_reasons':result.get('degraded_reasons',[]),
            'expansion_error':result.get('expansion_error')}
