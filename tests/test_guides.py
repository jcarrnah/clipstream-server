from datetime import datetime, timezone

from app.guides import filler, guide_key, match

UTC = timezone.utc
ENTRIES = [
    {"id": 1, "tvg_id": "WPIX.us", "name": "US| CW PIX11 (WPIX) NEW YORK", "epg_source": 1},
    {"id": 2, "tvg_id": "loc.CBS (KPIX) San Francisco, CA", "name": "US| CBS 5 SAN FRANCISCO CA (KPIX)", "epg_source": 1},
    {"id": 3, "tvg_id": "NBCNewsNow.us", "name": "PRIME| NBC NEWS NOW ᴿᴬᵂ", "epg_source": 1},
    {"id": 4, "tvg_id": "band.br", "name": "BR - BAND HD", "epg_source": 4},
    {"id": 5, "tvg_id": "Band.br", "name": "BR - BAND CAMPINAS HD", "epg_source": 2},
    {"id": 6, "tvg_id": "band.pt", "name": "PT - BAND HD", "epg_source": 2},
    {"id": 7, "tvg_id": "band2.br", "name": "BR| BAND", "epg_source": 2},
    {"id": 8, "tvg_id": "WNET-DT.us_locals1", "name": "WNET-DT", "epg_source": 5},
    {"id": 9, "tvg_id": "WWE.us", "name": "US| WWE", "epg_source": 1},
]


def test_keys_drop_prefixes_and_quality():
    assert guide_key("US - NBC NEWS NOW UHD") == ("US", "NBC NEWS NOW")
    assert guide_key("PRIME| NBC NEWS NOW ᴿᴬᵂ") == (None, "NBC NEWS NOW")
    assert guide_key("USA| ESPN HD") == ("US", "ESPN")


def test_call_signs_match_whatever_the_name():
    assert match("US - PIX11 (WPIX) NEW YORK HD", ENTRIES, {})["id"] == 1
    assert match("US - PBS (WNET) NEW YORK HD", ENTRIES, {})["id"] == 8
    assert match("US - PBS (WLIW) NEW YORK HD", ENTRIES, {}) is None


def test_names_match_in_a_compatible_country_preferring_the_ranked_source():
    assert match("US - NBC NEWS NOW UHD", ENTRIES, {})["id"] == 3
    assert match("BR| BAND HD", ENTRIES, {4: 0, 2: 1})["id"] == 4
    assert match("BR| BAND HD", ENTRIES, {2: 0, 4: 1})["id"] == 7


def t(h, m=0):
    return datetime(2026, 10, 10, h, m, tzinfo=UTC)


def test_filler_is_hourly_blocks_with_the_channel_name():
    blocks = filler("Lifetime", None, t(1, 30), t(4))
    assert [(b["start"], b["end"]) for b in blocks] == [
        ("2026-10-10T01:30:00Z", "2026-10-10T02:00:00Z"), ("2026-10-10T02:00:00Z", "2026-10-10T03:00:00Z"),
        ("2026-10-10T03:00:00Z", "2026-10-10T04:00:00Z")]
    assert all(b["placeholder"] and b["title"] == "Lifetime" for b in blocks)


def test_filler_puts_the_event_in_its_slot():
    event = {"title": "Browns @ Jets", "start": "2026-10-10T02:00:00Z", "end": "2026-10-10T05:00:00Z", "status": "upcoming"}
    blocks = filler("NFL Game 08", event, t(1, 30), t(4))
    assert [b["title"] for b in blocks] == ["Coming up: Browns @ Jets", "Browns @ Jets"]
    assert blocks[1]["end"] == "2026-10-10T05:00:00Z" and not blocks[1]["placeholder"]


def test_event_without_a_time_fills_the_window():
    blocks = filler("Live Event 01", {"title": "NO EVENT", "start": None, "end": None, "status": "unknown"}, t(1), t(3))
    assert len(blocks) == 1 and blocks[0]["title"] == "No event scheduled"


def test_after_the_event_the_slot_is_off_air():
    event = {"title": "Browns @ Jets", "start": "2026-10-09T17:00:00Z", "end": "2026-10-09T20:00:00Z", "status": "ended"}
    assert {b["title"] for b in filler("NFL Game 08", event, t(1), t(3))} == {"Off air"}
