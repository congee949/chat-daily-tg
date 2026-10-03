"""Versioned rubric candidates, evidence-bound human review and explicit activation."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import difflib
import json
import os
from pathlib import Path
import tempfile

from chat_daily_tg.call_receipts import digest
from chat_daily_tg.content_replay import verify


def atomic_json(path,value):
    atomic_text(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def atomic_text(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


class RubricCandidates:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)

    @contextmanager
    def lock(self):
        with (self.root/'.lock').open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def _path(self,identity):
        if len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('invalid candidate id')
        return self.root/(identity+'.json')

    def read(self,identity):
        row = json.loads(self._path(identity).read_text())
        if (row.get('id') != identity or digest(row['text']) != row['text_hash']
                or digest(row['parent_text']) != row['parent_hash']
                or digest([row['parent_hash'], row['text_hash'], row['feedback_ids']]) != identity):
            raise ValueError('candidate content identity mismatch')
        return row

    def draft(self,*,parent,text,feedback_ids,reason):
        if len(text.strip())<40 or not text.startswith('# 成长卡片评审偏好 v') or not reason or not feedback_ids:
            raise ValueError('complete rubric, feedback ids and reason required')
        identity=digest([digest(parent),digest(text),feedback_ids])
        with self.lock():
            if self._path(identity).exists():return self.read(identity)
            row={'schema':'rubric-candidate.v1','id':identity,'parent_hash':digest(parent),
                 'parent_text':parent,'text':text,'text_hash':digest(text),'feedback_ids':feedback_ids,
                 'reason':reason,'state':'draft','created_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(self._path(identity),row)
            changes = ''.join(difflib.unified_diff(parent.splitlines(keepends=True),
                text.splitlines(keepends=True), fromfile='parent', tofile='candidate'))
            atomic_text(self.root / (identity + '.diff'), changes)
        return row

    def evaluated(self,identity,manifest,report):
        verify(manifest)
        with self.lock():
            row=self.read(identity)
            if row['state']!='draft':raise ValueError('candidate must be draft')
            growth=[s for s in manifest['samples'] if s.get('source')=='growth' and s.get('human_label') is not None]
            if len(growth)<20:raise ValueError('at least 20 labelled real growth samples required')
            if report.get('manifest_hash')!=manifest['manifest_hash'] or not report.get('evaluation_id'):
                raise ValueError('evaluation manifest mismatch')
            rules=report.get('rules',[])
            if len(rules)!=2 or rules[0].get('prompt_hash')!=row['parent_hash'] or rules[1].get('prompt_hash')!=row['text_hash']:
                raise ValueError('evaluation rubric hashes mismatch')
            results=report.get('rows',[])
            expected={(s['sample_id'],r['version']) for s in manifest['samples'] if s['split']==report.get('split') for r in rules}
            observed={(r.get('sample_id'),r.get('rule',{}).get('version')) for r in results}
            if expected!=observed or not expected or any(r.get('status')!='succeeded' for r in results):
                raise ValueError('incomplete or failed evaluation')
            if len(results)!=len(expected) or rules[0]['version']==rules[1]['version']:
                raise ValueError('duplicate evaluation rows or rule versions')
            by_sample={s['sample_id']:s for s in manifest['samples']}
            by_version={r['version']:r for r in rules}
            for result in results:
                sample=by_sample[result['sample_id']]
                if (result.get('input_hash')!=sample['text_hash']
                        or result.get('content_id')!=sample['content_id']
                        or result.get('source_ref')!=sample['source_ref']
                        or result.get('human_label')!=sample.get('human_label')
                        or result['rule']!=by_version[result['rule']['version']]):
                    raise ValueError('evaluation row differs from frozen evidence')
            from chat_daily_tg.content_replay import replay
            lookup={(r['sample_id'],r['rule']['version']):r['result'] for r in results}
            checked=replay(manifest,rules=rules,split=report['split'],holdout_contract=report.get('holdout_contract'),
                           evaluate=lambda s,r:lookup[(s['sample_id'],r['version'])])
            if (checked['evaluation_id']!=report['evaluation_id']
                    or checked['metrics']!=report.get('metrics')
                    or {d['sample_id'] for d in checked['disagreements']}!={d['sample_id'] for d in report.get('disagreements',[])}):
                raise ValueError('evaluation identity or summary inconsistent')
            compared={s['sample_id'] for s in growth if s['split']==report.get('split')}
            if len(compared)<20:raise ValueError('evaluation needs 20 labelled growth samples in selected split')
            for s in growth:
                if s['sample_id'] in compared and any(type(s.get(k)) is not bool for k in ('too_long','meaning_lost','overclaimed')):
                    raise ValueError('growth labels require length, meaning and overclaim review')
            misses=[r['sample_id'] for r in results if r['rule']==rules[1] and r.get('human_label')=='worth_sending' and r['result']['decision']=='omit']
            row.update(state='evaluated',evaluation_id=report['evaluation_id'],evaluation_hash=digest(report),
                       manifest_hash=manifest['manifest_hash'],required_explanations=misses,
                       review_top5=sorted(checked['disagreements'],key=lambda r:(r['sample_id'] not in misses,
                           not by_sample[r['sample_id']].get('meaning_lost',False)))[:5])
            atomic_json(self.root/(identity+'.evaluation.json'),report)
            atomic_json(self._path(identity),row)
            return row

    def review(self,identity,*,actor,decision,reason,explanations):
        if not actor or not reason or decision not in {'approve','reject'}:raise ValueError('human decision required')
        with self.lock():
            row=self.read(identity)
            if row['state']!='evaluated':raise ValueError('evaluate candidate first')
            if decision=='approve' and any(not explanations.get(s) for s in row['required_explanations']):
                raise ValueError('explain each worthwhile omission')
            row.update(state='reviewed' if decision=='approve' else 'rejected',
                       review={'actor':actor,'decision':decision,'reason':reason,'explanations':explanations,
                               'evaluation_id':row['evaluation_id']})
            atomic_json(self._path(identity),row);return row

    def activate(self,identity,active_path):
        with self.lock():
            row=self.read(identity);active_path=Path(active_path)
            if row['state']!='reviewed':raise ValueError('human review required')
            if digest(active_path.read_text())!=row['parent_hash']:raise ValueError('parent changed; replay again')
            report=json.loads((self.root/(identity+'.evaluation.json')).read_text())
            if digest(report)!=row['evaluation_hash']:raise ValueError('evaluation changed')
            # Rollback evidence is durable before the active file is replaced.
            atomic_json(self.root/'rollback.json',{'candidate_id':identity,'previous_text':row['parent_text'],
                        'active_hash':row['text_hash'],'previous_hash':row['parent_hash']})
            atomic_text(active_path,row['text'])
            row['state']='active';atomic_json(self._path(identity),row);return row

    def rollback(self,active_path,*,actor,reason):
        if not actor or not reason:raise ValueError('rollback actor and reason required')
        with self.lock():
            pointer=json.loads((self.root/'rollback.json').read_text());active_path=Path(active_path)
            if digest(active_path.read_text())!=pointer['active_hash']:raise ValueError('active rubric changed')
            atomic_text(active_path,pointer['previous_text'])
            row=self.read(pointer['candidate_id']);row.update(state='reviewed',rollback={'actor':actor,'reason':reason})
            atomic_json(self._path(row['id']),row);return row


def candidate_summaries(root, *, limit=5):
    """Read bounded candidate metadata for reports without creating state."""
    root = Path(root)
    if not root.exists():
        return []
    candidates = []
    for path in root.glob('*.json'):
        if len(path.stem) != 64:
            continue
        try:
            row = json.loads(path.read_text())
            if (row.get('schema') != 'rubric-candidate.v1'
                    or row.get('id') != path.stem
                    or digest(row['text']) != row['text_hash']
                    or digest(row['parent_text']) != row['parent_hash']):
                continue
            changes = list(difflib.unified_diff(row['parent_text'].splitlines(),
                                               row['text'].splitlines(), lineterm=''))
            added = sum(line.startswith('+') and not line.startswith('+++') for line in changes)
            removed = sum(line.startswith('-') and not line.startswith('---') for line in changes)
            evaluation = root / (row['id'] + '.evaluation.json')
            evaluated = False
            if evaluation.is_file() and row.get('evaluation_hash'):
                evaluated = digest(json.loads(evaluation.read_text())) == row['evaluation_hash']
            candidates.append({'id': row['id'], 'state': row['state'],
                'created_at': row['created_at'], 'added_lines': added, 'removed_lines': removed,
                'candidate_path': str(path), 'diff_path': str(root / (row['id'] + '.diff')),
                'evaluation_path': str(evaluation) if evaluated else None,
                'evaluation_id': row.get('evaluation_id') if evaluated else None,
                'evaluation_status': 'available' if evaluated else 'pending_or_unavailable'})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(candidates, key=lambda r: (r['created_at'], r['id']), reverse=True)[:limit]
