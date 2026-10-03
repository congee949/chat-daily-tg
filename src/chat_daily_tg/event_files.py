"""Human-confirmed event dossiers, rebuilt from immutable source versions."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.rubric_candidates import RubricCandidates, atomic_json, atomic_text


def validate_source(source):
    from datetime import datetime
    from urllib.parse import urlparse
    for field in ('content_id','text','url','publisher','published_at','reason','relation'):
        if not isinstance(source.get(field), str) or not source[field].strip():
            raise ValueError('source missing ' + field)
    url = urlparse(source['url'])
    if url.scheme not in {'http','https'} or not url.netloc or url.username or url.password:
        raise ValueError('source URL must be an HTTP reference without credentials')
    datetime.fromisoformat(source['published_at'].replace('Z','+00:00'))
    if source['relation'] not in {'same_event','same_topic_new_event'}:
        raise ValueError('invalid relation')
    facts = source.get('facts', [])
    if not isinstance(facts, list):raise ValueError('facts must be a list')
    for fact in facts:
        if (not isinstance(fact, dict) or not isinstance(fact.get('quote'), str)
                or not fact['quote'] or fact['quote'] not in source['text']):
            raise ValueError('fact lacks a verbatim source anchor')
    if not isinstance(source.get('conflicts', []), list):
        raise ValueError('conflicts must be a list')


class EventFiles(RubricCandidates):
    def create(self,*,title,actor):
        if not title or not actor:raise ValueError('explicit followed event and actor required')
        key=digest([title,actor])
        with self.lock():
            path=self.root/(key+'.event.json')
            if not path.exists():atomic_json(path,{'id':key,'title':title,'followed_by':actor,'state':'following','sources':[],'history':[]})
        return key

    def event(self,key):
        self._path(key)
        row = json.loads((self.root/(key+'.event.json')).read_text())
        if row.get('id') != key:raise ValueError('event identity mismatch')
        for source in row['sources']:
            validate_source(source)
            if (source.get('text_hash') != digest(source['text'])
                    or source.get('version') != digest([source['content_id'],source['text']])):
                raise ValueError('source content identity mismatch')
        return row

    def propose(self,key,source):
        validate_source(source)
        with self.lock():
            row=self.event(key);version=digest([source['content_id'],source['text']])
            correction = source.get('correction_of')
            if correction and (correction == version or not any(s['version']==correction for s in row['sources'])):
                raise ValueError('correction must reference an existing different source version')
            if any(s['version']==version for s in row['sources']):return version
            row['sources'].append({**source,'version':version,'text_hash':digest(source['text']),
                                   'state':'proposed','upstream_group':None,'upstream_status':'unknown'})
            atomic_json(self.root/(key+'.event.json'),row)
        return version

    def suggest(self, key, source, *, llm):
        """Run the existing judge against reviewed sources and save a proposal.

        A negative same-event verdict cannot establish same-topic membership.
        Suggestions never change an existing human decision or delivery state.
        """
        from dataclasses import asdict
        from datetime import datetime, timezone
        from chat_daily_tg.topic_dedup import IndexedMsg, SameEventJudge
        for field in ('content_id', 'text', 'url', 'publisher', 'published_at'):
            if not isinstance(source.get(field), str) or not source[field].strip():
                raise ValueError('source missing ' + field)
        row = self.event(key)
        if row.get('state','following')!='following':
            return {'event_id':key,'status':row['state'],'model_calls':0}
        version = digest([source['content_id'], source['text']])
        existing = next((s for s in row['sources'] if s['version'] == version), None)
        if existing and existing['state'] in {'confirmed', 'rejected'}:
            return {'event_id':key,'version':version,'status':'human_decision_preserved',
                    'state':existing['state'],'model_calls':0}
        confirmed = sorted([s for s in row['sources'] if s['state']=='confirmed'],
                           key=lambda s:s['published_at'], reverse=True)[:3]
        context_hash = digest([(s['version'],s['text_hash']) for s in confirmed])
        identity = digest([key, version, context_hash, 'same-event-suggestion.v1'])
        result = {'schema':'event-suggestion.v1','id':identity,'event_id':key,
                  'version':version,'content_id':source['content_id'],'source_url':source['url'],
                  'input_hash':digest(source['text']),'context_hash':context_hash,
                  'context_versions':[s['version'] for s in confirmed],
                  'created_at':datetime.now(timezone.utc).isoformat(),
                  'model':getattr(llm,'model',None),'model_calls':0}
        if not confirmed:
            result.update(status='needs_seed_review',relation='undetermined')
        else:
            matches = [IndexedMsg(i, s['published_at'], s['publisher'], s['text'], s['text'], None)
                       for i,s in enumerate(confirmed,1)]
            verdict = SameEventJudge(llm).judge(source['text'], matches)
            result.update(model_calls=1, verdict=asdict(verdict),
                          status='proposed' if verdict.ok else 'judge_failed',
                          relation='same_event' if verdict.ok and verdict.same_event else 'undetermined')
            if verdict.ok and verdict.same_event:
                # propose() is idempotent and preserves reviewed membership.
                self.propose(key, {**source,'relation':'same_event','reason':verdict.reason or 'same-event judge proposal'})
        with self.lock():
            # Each actual call retains a distinct receipt; never overwrite a failure.
            import uuid
            path = self.root / (identity + '.' + uuid.uuid4().hex + '.suggestion.json')
            atomic_json(path, result)
        return {**result,'receipt_path':str(path)}

    def decide(self,key,version,*,actor,decision,reason,upstream_group=None):
        if not actor or not reason or decision not in {'confirmed','rejected'}:raise ValueError('human decision required')
        with self.lock():
            row=self.event(key);source=next(s for s in row['sources'] if s['version']==version)
            if decision=='confirmed' and source['relation']!='same_event':raise ValueError('new event needs separate dossier')
            row['history'].append({'version':version,'previous':source['state'],'decision':decision,'actor':actor,'reason':reason})
            source.update(state=decision,reviewed_by=actor,review_reason=reason,
                          upstream_group=upstream_group,upstream_status='confirmed' if upstream_group else 'unknown')
            atomic_json(self.root/(key+'.event.json'),row)
        return self.rebuild(key)

    def set_status(self,key,*,state,actor,reason):
        if state not in {'following','paused','closed'} or not actor or not reason:
            raise ValueError('explicit event status, actor and reason required')
        with self.lock():
            row=self.event(key)
            previous=row.get('state','following')
            if previous!=state:
                row['history'].append({'kind':'status','previous':previous,'state':state,
                    'actor':actor,'reason':reason,'at':datetime.now(timezone.utc).isoformat()})
                row['state']=state
                atomic_json(self.root/(key+'.event.json'),row)
        return self.rebuild(key)

    def rebuild(self,key):
        with self.lock():
            row=self.event(key)
            sources=sorted([s for s in row['sources'] if s['state']=='confirmed'],key=lambda s:s['published_at'])
            identity=digest([(s['version'],s.get('facts',[]),s.get('correction_of'),s.get('conflicts',[]),s.get('upstream_group')) for s in sources])
            progress_path=self.root/(key+'.progress.json')
            progress=json.loads(progress_path.read_text()) if progress_path.exists() else {'active_facts':[],'items':[]}
            facts={}
            for source in sources:
                for fact in source.get('facts',[]):
                    fact_key='fact:'+digest(fact['quote'])
                    facts[fact_key]={'kind':'fact','quote':fact['quote'],'content_id':source['content_id'],
                                     'text_hash':source['text_hash'],'source_version':source['version']}
                for conflict in source.get('conflicts',[]):
                    facts['conflict:'+digest(conflict)]={'kind':'conflict','detail':conflict,'content_id':source['content_id'],'text_hash':source['text_hash']}
                if source.get('correction_of'):
                    facts['correction:'+source['version']]={'kind':'correction','content_id':source['content_id'],
                        'text_hash':source['text_hash'],'correction_of':source['correction_of']}
            previous=set(progress['active_facts']);current=set(facts)
            if current!=previous:
                progress['items'].append({'at':datetime.now(timezone.utc).isoformat(),
                    'summary_version':identity,'added':[facts[k] for k in sorted(current-previous)],
                    'removed':sorted(previous-current)})
                progress['active_facts']=sorted(current)
                atomic_json(progress_path,progress)
            lines=['# '+row['title'],'','状态：'+row.get('state','following'),'','综述版本：'+identity,'',
                   '最近来源时间：'+(sources[-1]['published_at'] if sources else '暂无确认来源'),
                   '事实进展记录：'+str(len(progress['items']))+' 条','', 
                   f"发布渠道：{len({s['publisher'] for s in sources})}；已确认独立出处：{len({s['upstream_group'] for s in sources if s['upstream_status']=='confirmed'})}；未知出处：{sum(s['upstream_status']=='unknown' for s in sources)}",'']
            for source in sources:
                lines.extend([f"## {source['published_at']} · {source['publisher']}",'',
                              f"[原文]({source['url']}) · 内容版本 `{source['version']}`",''])
                # Facts must quote an exact source excerpt; no model-invented synthesis.
                for fact in source.get('facts',[]):
                    if not fact.get('quote') or fact['quote'] not in source['text']:
                        raise ValueError('fact lacks a verbatim source anchor')
                    lines.append('- '+fact['quote'])
                if not source.get('facts'):lines.append('事实摘要待复审。')
                if source.get('correction_of'):lines.append('更正内容版本：'+source['correction_of'])
                for conflict in source.get('conflicts',[]):lines.append('冲突待核对：'+str(conflict))
                lines.append('')
            confirmed_snapshot = '\n'.join(lines)
            pending = [s for s in row['sources'] if s['state']=='proposed']
            if pending:
                lines.extend(['## 待核实关联', '', '以下为已收录的来源候选，尚未确认事件归属。', ''])
                for source in sorted(pending, key=lambda s:s['published_at']):
                    lines.extend([f"### {source['published_at']} · {source['publisher']}", '',
                                  f"[来源]({source['url']}) · 内容版本 `{source['version']}`", '',
                                  source['reason'], '',
                                  '时间依据：' + source.get('timestamp_basis', 'source published_at'), '',
                                  source['text'], ''])
                    if source.get('target_url'):
                        lines.extend([f"[我的推送]({source['target_url']})", ''])
                    if source.get('external_url') and source['external_url'] != source['url']:
                        lines.extend([f"[外部原文]({source['external_url']})", ''])
            path=self.root/(key+'.md');atomic_text(path,'\n'.join(lines))
            version_path=self.root/(key+'.'+identity+'.md')
            if not version_path.exists():atomic_text(version_path,confirmed_snapshot)
            return {'event_id':key,'summary_version':identity,'path':str(path),'source_count':len(sources),'state':row.get('state','following'),'progress_count':len(progress['items'])}
