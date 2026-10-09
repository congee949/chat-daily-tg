"""Local content-iteration commands; JSON requests are explicit audit inputs."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from chat_daily_tg.content_replay import freeze, replay, write_frozen, source_quality
from chat_daily_tg.rubric_candidates import RubricCandidates, atomic_json
from chat_daily_tg.content_operations import DeliveryReview, health_report
from chat_daily_tg.event_files import EventFiles
from chat_daily_tg.value_profiles import rank_candidates, blind_comparison

COMMANDS=('freeze','replay','rubric-draft','rubric-evaluate','rubric-review','rubric-activate','rubric-rollback',
          'event-create','event-status','event-suggest','event-propose','event-decide','event-rebuild','review-add','review-decide',
          'review-report','health','rank','blind','source-quality','calls-report','calls-export','daily-review','value-preview','blind-prepare','blind-result','model-replay','health-notify','health-collect','fetch-health','reply-review','growth-review','source-weekly','daily-candidates','daily-preview','feedback-report')


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def execute(command,request,root):
    if command=='feedback-report':
        from chat_daily_tg.feedback_report import feedback_report
        return feedback_report(root=root)
    if command=='daily-candidates':
        from chat_daily_tg.daily_candidates import daily_candidates
        return daily_candidates(**request)
    if command=='daily-preview':
        from chat_daily_tg.daily_candidates import daily_candidates
        from chat_daily_tg.config import load_config
        from chat_daily_tg.application import _llm_from_block
        from chat_daily_tg.value_profiles import preview
        extraction=daily_candidates(request['archive_dir'],bindings=request.get('bindings'))
        if not extraction['candidates']:
            return {**extraction,'status':'needs_source_mapping','model_calls':0}
        cfg=load_config(Path(request['config']))
        llm=_llm_from_block(cfg,cfg.resolve_model_alias(request['model_alias']))
        try:
            result=preview(extraction['candidates'],llm=llm,task=request['task'],
                           selection_count=request.get('selection_count',10))
        finally:llm.close()
        return {'extraction':extraction,'preview':result}
    if command=='source-weekly':
        from chat_daily_tg.source_quality import weekly_quality
        return weekly_quality(**request)
    if command=='growth-review':
        from chat_daily_tg.growth_store import review_ambiguous
        return review_ambiguous(**request)
    if command=='reply-review':
        from chat_daily_tg.content_feedback import ReplyIntake
        request=dict(request)
        intake=ReplyIntake(root,**request.pop('intake'))
        return intake.review_response(**request)
    if command=='fetch-health':
        from chat_daily_tg.content_health import collect_fetch_health
        return collect_fetch_health(**request)
    if command=='health-collect':
        from chat_daily_tg.content_health import collect_health
        return collect_health(**request)
    if command=='health-notify':
        from chat_daily_tg.content_operations import notify_health
        return notify_health(root,read(request['health']))
    if command=='blind-prepare':
        from chat_daily_tg.value_profiles import blind_bundle
        bundle=blind_bundle(**request)
        root=Path(root)
        for name,payload in bundle.items():
            path=root/(name+'.json')
            if path.exists() and read(path)!=payload:
                raise ValueError('blind artifact already frozen; use a new root')
        for name,payload in bundle.items():
            atomic_json(root/(name+'.json'),payload)
        return {'review':str(root/'review.json'),'private':str(root/'private.json')}
    if command=='blind-result':
        from chat_daily_tg.value_profiles import blind_result
        request=dict(request)
        request['review']=read(request['review'])
        request['private']=read(request['private'])
        return blind_result(**request)
    if command=='model-replay':
        from chat_daily_tg.config import load_config
        from chat_daily_tg.application import _llm_from_block
        from chat_daily_tg.content_replay import model_replay
        request=dict(request)
        cfg=load_config(Path(request.pop('config')))
        alias=request.pop('model_alias')
        request['manifest']=read(request['manifest'])
        request['rules']=read(request['rules'])
        llm=_llm_from_block(cfg,cfg.resolve_model_alias(alias))
        try:
            return model_replay(**request,llm=llm,output_root=root)
        finally:
            llm.close()
    if command=='value-preview':
        from chat_daily_tg.config import load_config
        from chat_daily_tg.application import _llm_from_block
        from chat_daily_tg.value_profiles import preview
        request=dict(request)
        cfg=load_config(Path(request.pop('config')))
        alias=request.pop('model_alias')
        llm=_llm_from_block(cfg,cfg.resolve_model_alias(alias))
        try:
            return preview(**request,llm=llm)
        finally:
            llm.close()
    if command=='daily-review':
        from chat_daily_tg.content_operations import daily_review
        return daily_review(root, **request)
    if command=='calls-export':
        from chat_daily_tg.call_receipts import CallReceipts
        return CallReceipts(root).export_response(**request)
    if command=='calls-report':
        from chat_daily_tg.call_receipts import CallReceipts
        return CallReceipts(root).report(**request)
    if command=='freeze':return freeze(**request)
    if command=='replay':
        manifest=read(request['manifest']);rules=read(request['rules']);predictions=read(request['predictions'])
        lookup={(r['sample_id'],r['rule_version']):r for r in predictions}
        if len(lookup)!=len(predictions):raise ValueError('duplicate predictions')
        def evaluate(sample,rule):
            row=lookup[(sample['sample_id'],rule['version'])]
            if row['input_hash']!=sample['text_hash'] or row['rule']!=rule:raise ValueError('prediction identity mismatch')
            return row['result']
        return replay(manifest,rules=rules,evaluate=evaluate,split=request.get('split','development'),
                      holdout_contract=request.get('holdout_contract'))
    if command.startswith('rubric-'):
        store=RubricCandidates(root);op=command.removeprefix('rubric-');request=dict(request)
        if op=='draft':
            request['parent']=Path(request.pop('parent_path')).read_text()
            request['text']=Path(request.pop('candidate_path')).read_text()
        if op=='evaluate':
            request['manifest']=read(request['manifest']);request['report']=read(request['report'])
            op='evaluated'
        return getattr(store,op)(**request)
    if command=='event-status':
        return EventFiles(root).set_status(**request)
    if command=='event-suggest':
        from chat_daily_tg.config import load_config
        from chat_daily_tg.application import _llm_from_block
        request=dict(request)
        cfg=load_config(Path(request.pop('config')))
        alias=request.pop('model_alias')
        llm=_llm_from_block(cfg,cfg.resolve_model_alias(alias))
        try:
            return EventFiles(root).suggest(**request,llm=llm)
        finally:
            llm.close()
    if command.startswith('event-'):
        return getattr(EventFiles(root),command.removeprefix('event-'))(**request)
    if command.startswith('review-'):
        return getattr(DeliveryReview(root),command.removeprefix('review-'))(**request)
    if command=='health':return health_report(**request)
    if command=='rank':return rank_candidates(**request)
    if command=='blind':return blind_comparison(**request)
    if command=='source-quality':return source_quality(**request)
    raise ValueError('unknown command')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=COMMANDS)
    parser.add_argument('--request',type=Path,required=True,help='JSON request; use {} for review-report')
    parser.add_argument('--root',type=Path,required=True,help='explicit local output/state directory')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=execute(args.command,read(args.request),args.root)
        if args.command=='freeze':write_frozen(args.output,result)
        else:atomic_json(args.output,result)
    except (ValueError,KeyError,OSError,TypeError) as exc:
        parser.exit(2,f'{type(exc).__name__}: {exc}\n')
    print(json.dumps({'command':args.command,'output':str(args.output)},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
