"""Build a feedback-original catalog from validated Podcast4Bot provenance."""
import hashlib
import json
from pathlib import Path

from chat_daily_tg.knowledge_sources import load_media_ledger, load_podcast
from chat_daily_tg.knowledge_index import canonical_url
from chat_daily_tg.rubric_candidates import atomic_text


def build_media_originals(*, podcast_root, media_ledger, output_path):
    ledger,_=load_media_ledger(Path(media_ledger))
    documents,_=load_podcast(Path(podcast_root),ledger)
    rows=[]
    for document in documents:
        if document.mapping_status!='confirmed' or document.document_role!='original':continue
        if document.representation_type not in {'article','srt','transcript'}:continue
        matching=[r for r in ledger if canonical_url(str(r.get('url') or ''))==document.canonical_url]
        for producer,url in sorted({(r['producer'],r['url']) for r in matching}):
            rows.append({'schema':'media-feedback-original.v1','content_id':document.content_id,
                'producer':producer,'url':url,'source_ref':document.canonical_url,'title':document.title,
                'text':document.text,'content_hash':hashlib.sha256(document.text.encode()).hexdigest(),
                'verified_original':True,'verification_scope':'archive content and confirmed delivery association',
                'representation':document.representation_type,'transcription_quality':document.metadata.get('asr_quality'),
                'mapping_provenance':'knowledge_sources.load_podcast+media_sent_ledger'})
    rows.sort(key=lambda r:(r['content_id'],r['producer'],r['url']))
    atomic_text(output_path,''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    return {'schema':'media-originals-build.v1','output_path':str(output_path),'rows':len(rows),
            'content_count':len({r['content_id'] for r in rows}),
            'verification_scope':'provenance only; transcription accuracy requires review'}
