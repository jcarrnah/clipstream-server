from app.backups import ordered
from app.lineup import duplicates, new_numbers


def channel(id, name, group=1, number=None, pin=None, hidden=False, auto=True):
    return {"id": id, "name": name, "channel_group_id": group, "auto_created": auto, "streams": [id * 10],
            "hidden_from_output": hidden, "effective_channel_number": pin if pin is not None else number,
            "override": {"channel_number": pin} if pin is not None else None}


def test_same_named_channels_in_a_group_keep_the_first():
    chs = [channel(1342, "US - NHL TEAMS - NEW JERSEY DEVILS "), channel(1339, "US - NHL TEAMS - NEW JERSEY DEVILS"),
           channel(1340, "US - NHL TEAMS - NEW YORK RANGERS"), channel(1400, "US - NHL TEAMS - NEW JERSEY DEVILS", group=2)]
    assert duplicates(chs) == {1339: [1342]}


def test_manual_channels_are_not_twins():
    assert duplicates([channel(1, "ESPN"), channel(2, "ESPN", auto=False)]) == {}


def test_new_channels_pin_after_their_group():
    chs = [channel(1, "A", pin=500), channel(2, "B", pin=501), channel(3, "C", number=520), channel(4, "D", number=502),
           channel(5, "X", group=2, pin=502), channel(6, "Y", group=3, number=7)]
    # 502 is taken by group 2; group 3 has no pinned layout, so Dispatcharr keeps numbering it.
    assert new_numbers(chs, set()) == {3: 503, 4: 504}


def test_hidden_twins_get_no_number():
    chs = [channel(1, "A", pin=500), channel(2, "A", number=9), channel(3, "B", number=8, hidden=True)]
    assert new_numbers(chs, {2}) == {}


def test_twin_streams_follow_the_creating_stream():
    own = {"id": 1, "name": "US - NHL TEAMS - NEW YORK RANGERS ", "m3u_account": 5, "stream_id": "576221"}
    candidates = [{"id": 21, "name": "US - NHL TEAMS - NEW YORK RANGERS ", "m3u_account": 3, "stream_id": "576221"}]
    assert ordered(own, candidates, {5: 3, 3: 2}, [7]) == [1, 7, 21]
