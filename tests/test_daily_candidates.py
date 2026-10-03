from chat_daily_tg.daily_candidates import daily_candidates


def test_extract_unique_original_and_preserve_summary(tmp_path):
    summary='- First https://example.org/one\n- Unsupported claim\n- Ambiguous https://example.org/two\n'
    (tmp_path/'concise.md').write_text(summary)
    raw='[Telegram / group / 10:00 / A] Complete first original https://example.org/one\n\n[Telegram / group / 11:00 / B] Second https://example.org/two\n'
    (tmp_path/'telegram-group.md').write_text(raw)
    (tmp_path/'wechat-group.md').write_text('### 2026-09-29 12:00\n\nThird https://example.org/two\n')
    result=daily_candidates(tmp_path)
    assert len(result['candidates'])==1
    candidate=result['candidates'][0]
    assert 'Complete first original' in candidate['text']
    loc=candidate['source_locator']
    assert raw[loc['char_start']:loc['char_end']]==candidate['text']
    assert [u['reason'] for u in result['unmatched']]==['no_exact_link','ambiguous_originals']
    assert (tmp_path/'concise.md').read_text()==summary


def test_explicit_multisource_binding_checks_frozen_source(tmp_path):
    import pytest
    from chat_daily_tg.call_receipts import digest
    summary='- 综合判断，没有链接。\n\n## 其他栏目\n栏目正文'
    (tmp_path/'concise.md').write_text(summary)
    one='[Telegram / group / 10:00 / A] first original\n'
    two='### 2026-09-29 10:00\n\nsecond original\n'
    a=tmp_path/'telegram-a.md';a.write_text(one)
    b=tmp_path/'wechat-b.md';b.write_text(two)
    bindings={'summary_hash':digest(summary),'items':[{'summary_offset':0,'actor':'fixture','reason':'reviewed sources',
        'sources':[{'path':str(a),'char_start':0,'char_end':len(one),'archive_hash':digest(one)},
                   {'path':str(b),'char_start':0,'char_end':len(two),'archive_hash':digest(two)}]}]}
    result=daily_candidates(tmp_path,bindings)
    assert len(result['candidates'])==1 and result['unmatched']==[]
    item=result['candidates'][0]
    assert 'first original' in item['text'] and 'second original' in item['text']
    assert '其他栏目' not in item['candidate_output']
    assert len(item['source_locators'])==2
    a.write_text(one+'changed')
    with pytest.raises(ValueError,match='binding mismatch'):daily_candidates(tmp_path,bindings)
