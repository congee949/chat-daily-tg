import httpx
from chat_daily_tg.config import Triage
from chat_daily_tg.triage import triage_groups, INJECTION_MARK

def ans(value, inj):
    return {"answers": {"value": {"type": "score", "score": value}, "inject": {"type": "noul", "noul": inj}}}

def client_for(answers):
    it = iter(answers)
    def handler(req):
        a = next(it)
        return httpx.Response(500) if a is None else httpx.Response(200, json=a)
    return httpx.Client(transport=httpx.MockTransport(handler))

G = [("a", "x"), ("b", "y"), ("c", "z")]

def test_keep_drop_and_flag():
    kept, v = triage_groups(G, Triage(enabled=True), client_for([ans(1.5, .01), ans(.1, .02), ans(.1, .9)]))
    assert [k[0] for k in kept] == ["a", "c"]
    assert kept[1][1].startswith(INJECTION_MARK)
    assert v[2].flagged

def test_drop_flagged():
    kept, _ = triage_groups(G, Triage(enabled=True, drop_flagged=True), client_for([ans(2, .9), ans(2, 0), ans(2, 0)]))
    assert [k[0] for k in kept] == ["b", "c"]

def test_fail_open():
    kept, v = triage_groups(G, Triage(enabled=True), client_for([None, ans(2, 0), {"bad": 1}]))
    assert [k[0] for k in kept] == ["a", "b", "c"]
    assert v[0].error and v[2].error

def test_never_drop_all():
    kept, _ = triage_groups(G, Triage(enabled=True), client_for([ans(0, 0)] * 3))
    assert kept == G

def test_default_off():
    assert Triage().enabled is False
