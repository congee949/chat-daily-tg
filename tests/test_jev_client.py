import httpx
import pytest
from chat_daily_tg.jev_client import JevClient, JevError

Q = {'same_event': {'type': 'noul'}, 'new_info': {'type': 'choice', 'criteria': {'none': '', 'minor': '', 'substantial': '', 'uncertain': ''}}}
BODY = {'model': 'jev-1.13.0', 'answers': {'same_event': {'type': 'noul', 'noul': .8}, 'new_info': {'type': 'choice', 'choice': 'minor', 'probabilities': {'none': 0, 'minor': 1, 'substantial': 0, 'uncertain': 0}, 'confidence': 1}}, 'usage': {'input_tokens': 2, 'output_tokens': 3}}

def test_request_shape_and_parse():
    seen = {}
    def handler(request):
        seen.update(request=json.loads(request.content), auth=request.headers['authorization'])
        return httpx.Response(200, json=BODY)
    import json
    c = JevClient('secret', client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = c.evaluate(state={'new_card': 'x'}, questions=Q)
    assert r.answers['new_info']['choice'] == 'minor'
    assert seen['auth'] == 'Bearer secret'
    assert seen['request'] == {'model': 'jev-latest', 'state': {'new_card': 'x'}, 'questions': Q}

@pytest.mark.parametrize('status', [401, 403, 422])
def test_non_retryable_status(status):
    c = JevClient('secret', client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))))
    with pytest.raises(JevError) as e: c.evaluate(state={}, questions=Q)
    assert e.value.attempts == 1 and e.value.status_code == status

@pytest.mark.parametrize('status', [429, 529, 500, 501, 503, 599])
def test_retry_once_on_429(status):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status) if len(calls) == 1 else httpx.Response(200, json=BODY)
    c = JevClient('secret', client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.evaluate(state={}, questions=Q).attempts == 2
    assert len(calls) == 2

def test_malformed_probability():
    bad = dict(BODY); bad['answers'] = dict(BODY['answers']); bad['answers']['same_event'] = {'type': 'noul', 'noul': 2}
    c = JevClient('secret', client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=bad))))
    with pytest.raises(JevError, match='malformed'): c.evaluate(state={}, questions=Q)

def test_timeout_not_retried_and_logs_no_body(caplog):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('sensitive error body')
    c = JevClient('test-secret', client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(JevError, match='timeout'):
        c.evaluate(state={'text':'sensitive input'}, questions=Q)
    assert len(calls) == 1
    assert 'sensitive' not in caplog.text
    assert 'test-secret' not in repr(c)
