from wx_daily_tg.fingerprint import (
    extract_urls, extract_invite_codes, extract_md5s, extract_phones,
)


def test_extract_urls_basic():
    t = "check https://example.com/a?b=1 and http://x.y/z"
    urls = extract_urls(t)
    assert "https://example.com/a?b=1" in urls
    assert "http://x.y/z" in urls


def test_extract_urls_filters_cdn_and_image_hosts():
    t = "https://snsvideo.hk.wechat.com/foo and https://app.okx.com/en-us/join/49084340"
    urls = extract_urls(t)
    assert "https://app.okx.com/en-us/join/49084340" in urls
    assert "https://snsvideo.hk.wechat.com/foo" not in urls


def test_extract_invite_codes_with_context():
    t1 = "邀请码 PjGhDx 护照可注册"
    codes = extract_invite_codes(t1)
    assert "PjGhDx" in codes

    t2 = "这只是一段有大写BigWord和数字12345的无关文本"
    codes = extract_invite_codes(t2)
    assert codes == []


def test_extract_md5s_from_xml():
    xml = '<emoticonmd5>e61a83644f703e32377a4fe4f1d12fdb</emoticonmd5><cdnthumbmd5>58f01cd3c2f35f0dfdd29c8966fa98b1</cdnthumbmd5>'
    md5s = extract_md5s(xml)
    assert "e61a83644f703e32377a4fe4f1d12fdb" in md5s
    assert "58f01cd3c2f35f0dfdd29c8966fa98b1" in md5s


def test_extract_phones():
    t = "call 13812345678 or 15987654321, not 12345"
    phones = extract_phones(t)
    assert "13812345678" in phones
    assert "15987654321" in phones
    assert "12345" not in phones
