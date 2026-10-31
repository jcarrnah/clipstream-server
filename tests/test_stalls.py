from app.stalls import matching

EVENTS = [
    {"timestamp": "2026-10-25T21:26:33+00:00", "event_type": "channel_buffering", "channel_name": "F1| SKY SPORTS F1 FHD",
     "details": {"speed": 0.998, "stream_id": 1}},
    {"timestamp": "2026-10-25T21:26:48+00:00", "event_type": "channel_failover", "channel_name": "F1| SKY SPORTS F1 FHD",
     "details": {"reason": "buffering_timeout", "previous_provider_name": "area69-2", "duration": 15.0}},
    {"timestamp": "2026-10-25T21:26:49+00:00", "event_type": "client_connect", "channel_name": "F1| SKY SPORTS F1 FHD",
     "details": {"client_ip": "1.2.3.4"}},
    {"timestamp": "2026-10-25T21:26:40+00:00", "event_type": "channel_buffering", "channel_name": "US| WWE HD",
     "details": {"speed": 0.9}},
    {"timestamp": "2026-10-25T21:40:00+00:00", "event_type": "channel_failover", "channel_name": "F1| SKY SPORTS F1 FHD",
     "details": {"reason": "buffering_timeout"}},
]


def test_matches_the_channels_events_around_the_stall():
    stall = {"startedAt": "2026-10-25T21:26:47.5Z", "seconds": 4.2, "channel": "F1|  sky sports f1 fhd"}
    got = matching(stall, EVENTS)
    assert [e["type"] for e in got] == ["channel_buffering", "channel_failover"]
    assert got[0]["speed"] == 0.998
    assert got[1]["previous_provider_name"] == "area69-2"
    assert "duration" not in got[1]


def test_matches_by_the_provider_name_when_known():
    stall = {"startedAt": "2026-10-25T21:26:47.5Z", "seconds": 4.2, "channel": "Sky Sports F1",
             "providerName": "F1| SKY SPORTS F1 FHD"}
    assert len(matching(stall, EVENTS)) == 2


def test_bad_time_matches_nothing():
    assert matching({"startedAt": "soon", "seconds": 1, "channel": "F1| SKY SPORTS F1 FHD"}, EVENTS) == []
