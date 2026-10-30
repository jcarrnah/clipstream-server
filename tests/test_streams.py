from urllib.parse import parse_qs, urlparse

from app.streams import LINK_HOURS, secret_from, signed_url, valid

SECRET = secret_from("dispatcharr-password")
NOW = 1_800_000_000.0


def parts(url):
    u = urlparse(url)
    q = parse_qs(u.query)
    return u.path.rsplit("/", 1)[-1], q["exp"][0], q["sig"][0]


def test_signed_link_plays_until_it_expires():
    uuid, exp, sig = parts(signed_url("https://live.example.com", SECRET, "abc-123", now=NOW))
    assert uuid == "abc-123"
    assert valid(SECRET, uuid, exp, sig, now=NOW)
    assert valid(SECRET, uuid, exp, sig, now=NOW + (LINK_HOURS - 1) * 3600)
    assert not valid(SECRET, uuid, exp, sig, now=NOW + (LINK_HOURS + 1) * 3600)


def test_link_is_for_one_channel_and_one_secret():
    uuid, exp, sig = parts(signed_url("https://live.example.com", SECRET, "abc-123", now=NOW))
    assert not valid(SECRET, "other-channel", exp, sig, now=NOW)
    assert not valid(secret_from("another-password"), uuid, exp, sig, now=NOW)
    assert not valid(SECRET, uuid, str(int(exp) + 3600), sig, now=NOW)


def test_missing_or_bad_parameters():
    assert not valid(SECRET, "abc", None, "x", now=NOW)
    assert not valid(SECRET, "abc", "soon", "x", now=NOW)
    assert not valid(SECRET, "abc", str(int(NOW) + 60), None, now=NOW)


def test_link_stays_the_same_within_the_hour():
    a = signed_url("https://live.example.com", SECRET, "abc", now=NOW)
    b = signed_url("https://live.example.com", SECRET, "abc", now=NOW + 60)
    assert a == b


def test_secret_survives_restarts_and_can_be_set():
    assert secret_from("pw") == secret_from("pw")
    assert secret_from("pw", explicit="set") != secret_from("pw")
