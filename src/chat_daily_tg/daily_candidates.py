"""Source-bound candidates from an existing daily archive; no delivery writes."""
from pathlib import Path
import re
from chat_daily_tg.call_receipts import digest

_URL=re.compile(r'https?://[^\s<>"\u3002\uff0c]+')


def urls(text):
    return {u.rstrip(').,;，。；）') for u in _URL.findall(text)}


def daily_candidates(archive_dir, bindings=None):
    root=Path(archive_dir).resolve()
    summary=root/'concise.md'
    text=summary.read_text(encoding='utf-8')
    blocks=[]
    for path in sorted([*root.glob('wechat-*.md'),*root.glob('telegram-*.md')]):
        raw=path.read_text(encoding='utf-8')
        pattern=r'^### \d{4}-\d{2}-\d{2} \d{2}:\d{2}.*$' if path.name.startswith('wechat-') else r'^\[Telegram / [^\n]+\]'
        starts=[m.start() for m in re.finditer(pattern,raw,re.M)]
        for i,start in enumerate(starts):
            end=starts[i+1] if i+1<len(starts) else len(raw)
            original=raw[start:end]
            blocks.append({'text':original,'urls':urls(original),'path':str(path),
                           'start':start,'end':end,'line':raw[:start].count('\n')+1,
                           'archive_hash':digest(raw)})
    # A candidate is a top-level concise bullet including its continuation lines.
    starts=[m.start() for m in re.finditer(r'^[-*] ',text,re.M)]
    explicit={}
    if bindings is not None:
        if bindings.get('summary_hash')!=digest(text):raise ValueError('summary binding version mismatch')
        for binding in bindings['items']:
            offset=binding['summary_offset']
            if offset not in starts or offset in explicit:raise ValueError('invalid or duplicate summary offset')
            if not binding.get('actor') or not binding.get('reason'):raise ValueError('mapping attribution required')
            selected=[]
            for locator in binding['sources']:
                path=str(Path(locator['path']).resolve())
                matching=[b for b in blocks if b['path']==path and b['start']==locator['char_start']
                          and b['end']==locator['char_end'] and b['archive_hash']==locator['archive_hash']]
                if len(matching)!=1:raise ValueError('source block binding mismatch')
                if matching[0] in selected:raise ValueError('duplicate source block')
                selected.append(matching[0])
            if not selected:raise ValueError('empty source binding')
            explicit[offset]=(selected,binding)
    candidates=[];unmatched=[];seen=set()
    for i,start in enumerate(starts):
        end=starts[i+1] if i+1<len(starts) else len(text)
        heading=re.search(r'^#{1,6} ',text[start:end],re.M)
        if heading:end=start+heading.start()
        output=text[start:end].strip()
        links=urls(output)
        matches=[b for b in blocks if links & b['urls']]
        binding=None
        if start in explicit:matches,binding=explicit[start]
        if not matches or (len(matches)!=1 and binding is None):
            unmatched.append({'summary_offset':start,'candidate_output':output,
                              'reason':'no_exact_link' if not matches else 'ambiguous_originals',
                              'matching_blocks':len(matches)})
            continue
        identity=digest([[b['path'],b['start'],b['end'],b['text']] for b in matches])
        if identity in seen:continue
        seen.add(identity)
        locators=[{'path':b['path'],'char_start':b['start'],'char_end':b['end'],
                   'archive_hash':b['archive_hash']} for b in matches]
        source_ref='; '.join(b['path']+':'+str(b['line']) for b in matches)
        candidates.append({'content_id':'daily-original:'+identity,'text':'\n\n'.join(b['text'] for b in matches),
            'title':output.splitlines()[0][:120],'source_ref':source_ref,
            'candidate_output':output,'source_locator':locators[0],'source_locators':locators,
            'mapping_review':{'actor':binding['actor'],'reason':binding['reason']} if binding else None,
            'mapping_basis':'explicit reviewed block binding' if binding else 'unique exact URL shared by concise bullet and original message'})
    return {'schema':'daily-value-candidates.v1','archive_dir':str(root),
            'summary_hash':digest(text),'candidates':candidates,'unmatched':unmatched,
            'scope':'only uniquely source-linked concise bullets; original summary unchanged'}
