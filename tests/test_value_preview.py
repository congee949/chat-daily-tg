import json
from chat_daily_tg.value_profiles import preview, FIELDS

class Model:
    model='fixture'
    def __init__(self,fail_at=None):self.calls=0;self.fail_at=fail_at
    def chat(self,prompt,system):
        self.calls+=1
        if self.calls==self.fail_at:return '{broken',{}
        item=json.loads(prompt)
        return json.dumps({'content_type':'tool',**{field:{'status':'supported' if 'useful' in item['text'] else 'missing',
            'quote':item['text'],'source_ref':item['source_ref']} for field in FIELDS}}),{}

def pool():return [dict(content_id=str(i),text='useful' if i==2 else 'unknown',source_ref='ref:'+str(i)) for i in range(3)]

def test_preview_second_review_only_disagreements_and_boundary():
    model=Model();result=preview(pool(),llm=model,task='tools',selection_count=1)
    assert [c['content_id'] for c in result['candidate']]==['2','0','1']
    assert result['second_review_ids']==['2','0']
    assert result['assessment_calls']==5 and result['extra_assessment_calls']==2
    assert result['original']==pool() and result['delivery_order_changed'] is False
    assert result['network_attempts'] is None

def test_second_review_failure_keeps_original():
    result=preview(pool(),llm=Model(fail_at=4),task='tools',selection_count=1)
    assert result['fallback'] and result['candidate']==pool()
    assert result['receipts'][3]['status']=='failed'

def test_initial_failure_prevents_additional_review():
    result=preview(pool(),llm=Model(fail_at=1),task='tools',selection_count=1)
    assert result['fallback'] and result['extra_assessment_calls']==0


def test_blind_metrics_need_complete_pool_and_hide_versions():
    import pytest
    from chat_daily_tg.value_profiles import blind_bundle, blind_result
    items=[dict(content_id=str(i),text='body'+str(i),source_ref='ref',model='private clue') for i in range(12)]
    bundle=blind_bundle(items,list(reversed(items)),evaluation_id='fixture',
        contract={'selection_count':10,'severe_miss_definition':'labelled must read omitted','adoption_criteria':'human review'},extra_model_calls=4)
    assert 'private clue' not in json.dumps(bundle['review'])
    labels=[dict(content_id=str(i),useful=i%2==0,must_read=i==11,reason='fixture',content_type='tool') for i in range(12)]
    with pytest.raises(ValueError,match='complete pool'):
        blind_result(bundle['review'],bundle['private'],actor='fixture',labels=labels[:10],reading_seconds={'A':10,'B':11},choice='A',reason='fixture')
    result=blind_result(bundle['review'],bundle['private'],actor='fixture',labels=labels,reading_seconds={'A':10,'B':11},choice='tie',reason='fixture')
    metrics={row['version']:row for row in result['metrics'].values()}
    assert metrics['current']['must_read_misses']==['11']
    assert metrics['candidate']['must_read_misses']==[]
    assert metrics['candidate']['useful_count']==5
    assert result['adoption']=='requires_explicit_decision'
