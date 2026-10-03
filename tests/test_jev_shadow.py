import json
import pytest
from types import SimpleNamespace
from chat_daily_tg.jev_shadow import JevShadow, make_state
from chat_daily_tg.jev_client import JevResponse

def test_shadow_sanitizes_hashes_and_writes_only_result(tmp_path):
    class Client:
        retry_max_attempts = 1
        def evaluate(self, **kwargs):
            assert 'sk-secret' not in json.dumps(kwargs)
            return JevResponse('typesafe-ai/jev', {'same_event': {'probability': .9}, 'new_info': {'choice': 'minor', 'probabilities': {'none': 0, 'minor': 1, 'substantial': 0, 'uncertain': 0}}}, {'inputTokens': 2}, {}, 4)
    m = SimpleNamespace(msg_id=7, text='同一事件，authorization: Bearer sk-secret')
    p = tmp_path / 'new' / 'nested' / 'shadow.jsonl'
    JevShadow(Client(), path=p).observe('新卡片', [m], None, {'chat_id': '-1', 'msg_id': 8})
    row = json.loads(p.read_text())
    assert row['status'] == 'ok' and row['candidate_msg_ids'] == [7]
    assert row['input_sha256'] != ''

def test_zero_sample_rate_skips_observation(tmp_path):
    class Client:
        def evaluate(self, **kwargs):
            raise AssertionError('not called')
    matches = [SimpleNamespace(msg_id=7, text='事件文本足够长')]
    path = tmp_path / 'shadow.jsonl'
    JevShadow(Client(), path=path, sample_rate=0).observe('新卡片', matches, None)
    assert not path.exists()


@pytest.mark.parametrize('legacy_state', [None, 'invalid legacy budget'])
def test_shadow_failures_do_not_limit_later_observations(tmp_path, legacy_state):
    class Client:
        calls = 0

        def evaluate(self, **kwargs):
            self.calls += 1
            raise RuntimeError('failure')

    client = Client()
    path = tmp_path / 'shadow.jsonl'
    legacy_path = path.with_suffix('.budget.json')
    if legacy_state is not None:
        legacy_path.write_text(legacy_state)
    matches = [SimpleNamespace(msg_id=1, text='完整正文')]
    for _ in range(2):
        observer = JevShadow(client, path=path)
        for _ in range(30):
            observer.observe('新正文', matches, None)
    assert client.calls == 60
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 60 and all(row['status'] == 'error' for row in rows)
    if legacy_state is None:
        assert not legacy_path.exists()
    else:
        assert legacy_path.read_text() == legacy_state


def test_full_text_and_positions_only():
    text = '中文正文' * 1000
    matches = [SimpleNamespace(msg_id=i, text=text) for i in range(3)]
    state = make_state(text, matches)
    assert state['new_card'] == text
    assert all(c['text'] == text and set(c) == {'position', 'text'} for c in state['delivered_candidates'])
    with pytest.raises(ValueError):
        make_state(text, matches + matches)


@pytest.mark.parametrize('model,policy', [
    ({'enabled': False}, {}),
    ({'enabled': True, 'timeout': -1}, {}),
    ({'enabled': True, 'endpoint': 'https://invalid.example'}, {}),
    ({'enabled': True}, {'jev_shadow_sample_rate': 'invalid'}),
    ('invalid model block', {}),
])
def test_invalid_optional_config_only_disables_shadow(model, policy, monkeypatch, caplog):
    from chat_daily_tg.config import Models, DedupTopic
    from chat_daily_tg import jev_shadow
    models = Models(summary={'endpoint': 'https://example.test', 'model': 'test', 'api_key_env': 'TEST_KEY'}, jev=model)
    topic = DedupTopic(jev_shadow_enabled=True, **policy)
    cfg = SimpleNamespace(models=models, sources=SimpleNamespace(telegram=SimpleNamespace(dedup=SimpleNamespace(topic=topic))))
    before = topic.model_dump()
    def unexpected_client(**kwargs):
        pytest.fail('invalid optional config must not create an HTTP client')
    monkeypatch.setattr(jev_shadow, 'JevClient', unexpected_client)
    assert jev_shadow.build_shadow(cfg) is None
    assert topic.model_dump() == before
    assert 'initialization disabled' in caplog.text


@pytest.mark.parametrize('enabled', [False, 'true', 1, None])
def test_only_explicit_boolean_enables_shadow(enabled, monkeypatch):
    from chat_daily_tg import jev_shadow
    topic = SimpleNamespace(jev_shadow_enabled=enabled)
    cfg = SimpleNamespace(sources=SimpleNamespace(telegram=SimpleNamespace(dedup=SimpleNamespace(topic=topic))))
    # No models attribute: disabled shadow must return before model access.
    assert jev_shadow.build_shadow(cfg) is None
