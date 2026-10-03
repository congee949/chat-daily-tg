import json
import pytest
from chat_daily_tg.source_quality import weekly_quality


def test_weekly_separates_denominators_and_deduplicates(tmp_path):
    path=tmp_path/'fetch.jsonl'
    rows=[dict(schema='source-fetch.v1',machine='fixture',attempt_id=str(i),producer='tg',source_ref='channel',
        finished_at='2026-09-28T00:00:00+00:00',status=status) for i,status in enumerate(['no_update','failed','success'])]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    sample=dict(sample_id='1',source='tg',source_ref='channel',collected_at='2026-09-28T00:00:00+00:00',human_label=None)
    kwargs=dict(journals=[path,path],samples=[sample,sample],start='2026-09-22T00:00:00+00:00',end='2026-09-29T00:00:00+00:00')
    result=weekly_quality(**kwargs,markdown_path=tmp_path/'report.md')
    row=result['sources'][0]
    assert row['acquisition']['success_rate']==2/3
    assert row['usefulness']['rate'] is None and row['usefulness']['unlabelled']==1
    assert row['provenance']['unknown_items']==1
    assert not result['partial']
    kwargs['journals']=[tmp_path/'missing']
    assert weekly_quality(**kwargs)['partial']
    sample['human_label']='worth_sending'
    with pytest.raises(ValueError,match='attribution'):weekly_quality(**kwargs)
