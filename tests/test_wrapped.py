from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.history import History, parse_text, parse_time, resolve_title
from app.wrapped import (ItemInfo, looks_like_channel, Play, Wrapped, assemble, build_slides, genres_of, hours, longest_streak,
                         pair_events, year_stats)

TZ = ZoneInfo("America/New_York")


def at(y, m, d, h=20, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ).timestamp()


def test_parse():
    assert parse_time("2026-11-26T03:41:25.1233777Z") == datetime(2026, 11, 26, 3, 41, 25, 123377,
                                                                   tzinfo=ZoneInfo("UTC")).timestamp()
    assert parse_time("nonsense") is None
    assert parse_text("viewer2 has finished playing Reggie Dinkins - Dr. Watson's Dad on Roku Ultra") == \
        ("Reggie Dinkins - Dr. Watson's Dad", "Roku Ultra")
    assert parse_text("GARIBO22 is playing Resident Evil on LIVING ROOM") == ("Resident Evil", "LIVING ROOM")


def test_resolve_title():
    movies = {"missionimpossiblefallout": {"Id": "m1", "Name": "Mission: Impossible - Fallout"}}
    series = {"jujutsukaisen": {"Id": "s1"}, "starwarsandor": {"Id": "s2"}}
    assert resolve_title("Mission: Impossible - Fallout", movies, series)[1]["Id"] == "m1"
    assert resolve_title("JUJUTSU KAISEN - Hidden Inventory", movies, series)[:3:2] == ("Episode", "Hidden Inventory")
    assert resolve_title("Star Wars - Andor - One Way Out", movies, series)[1]["Id"] == "s2"
    assert resolve_title("Gone Show - Pilot", movies, series) is None


def test_sampler_counts_only_playing_time(tmp_path):
    h = History(str(tmp_path / "h.sqlite"), "http://jellyfin", "key")
    playing = [{"UserId": "u", "DeviceId": "d", "DeviceName": "TV", "NowPlayingItem": {"Id": "i"},
                "PlayState": {"IsPaused": False, "PositionTicks": 0}}]
    paused = [{**playing[0], "PlayState": {"IsPaused": True}}]
    h.record(playing, 1000)   # first poll: nothing counted yet
    h.record(playing, 1030)
    h.record(paused, 1060)    # paused: not counted
    h.record(playing, 1090)
    h.record(playing, 1500)   # Jellyfin didn't answer for 7 minutes: a new run, the gap isn't counted
    h.record(playing, 1530)
    seg = h.db.execute("SELECT count(*), sum(seconds) FROM segments").fetchone()
    assert seg == (1, 90)
    assert h.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
    h.record(playing, 1530 + 30 * 25)  # long after: a new segment (and run)
    assert h.db.execute("SELECT count(*) FROM segments").fetchone()[0] == 2
    h.db.close()


def test_pairs_and_assemble():
    events = [(1, "VideoPlayback", "u", "a", 100.0, "TV"), (2, "VideoPlayback", "u", "b", 150.0, "Phone"),
              (3, "VideoPlaybackStopped", "u", "a", 100.0 + 20000, "TV"),      # left open overnight
              (4, "VideoPlaybackStopped", "u", "x", 300.0, "TV"),              # no start: dropped
              (5, "VideoPlaybackStopped", "u", "b", 150.0 + 1800, "Phone")]
    pairs = pair_events(events)
    assert [(p[1], p[4] - p[3]) for p in pairs] == [("a", 20000), ("b", 1800)]
    infos = {"a": ItemInfo("Episode", "A", "s", "S", "s", runtime=1500), "b": ItemInfo("Movie", "B", runtime=6000)}
    plays = assemble(pairs, [], [], infos)
    assert [round(p.seconds) for p in plays] == [1650, 1800]  # capped at 110% of the runtime
    # The sampler saw "b": its time wins; a sampled segment with no log entry is a play too.
    segs = [("u", "b", "Phone", 200.0, 1900.0, 1200.0), ("u", "c", "TV", 5000.0, 6000.0, 900.0)]
    plays = assemble(pairs, segs, [], infos)
    assert sorted((p.item, round(p.seconds)) for p in plays) == [("a", 1650), ("b", 1200), ("c", 900)]
    # The sampler ran through "a" and never saw it play: it didn't happen.
    plays = assemble(pairs, segs[:1], [(0.0, 30000.0)], infos)
    assert [p.item for p in plays] == ["b"]


def test_stats_and_slides():
    infos = {f"e{i}": ItemInfo("Episode", f"E{i}", "s1", "Frieren", "s1", runtime=1440, genres=["Anime", "Fantasy"])
             for i in range(6)}
    infos["m"] = ItemInfo("Movie", "Dune", runtime=9000, genres=["Science Fiction"], year=2024, image="m")
    infos["h"] = ItemInfo("Episode", "H", "s2", "Hacks", "s2", runtime=1800, genres=["Comedy"])
    plays = [Play("me", f"e{i}", at(2026, 3, 7, 20) + 1200 * i, 1440, "TV") for i in range(6)]
    plays += [Play("me", "m", at(2026, 4, 1), 9000, "TV"), Play("me", "m", at(2026, 6, 1), 9000, "Phone"),
              Play("me", "m", at(2025, 12, 31), 9000, "TV"),  # last year: not counted
              Play("me", "h", at(2026, 3, 7, 21), 1800, "x86"),  # the emulator: not counted
              Play("you", "e0", at(2026, 5, 1), 1440, "TV"), Play("you", "h", at(2026, 5, 1), 1800, "TV"),
              Play("them", "e1", at(2026, 5, 2), 600, "TV")]
    stats = year_stats(plays, infos, 2026, TZ)
    me = stats["me"]
    assert me.seconds == 6 * 1440 + 2 * 9000 and me.episodes == 6 and me.movie_times == {"m": 2}
    assert me.days[date(2026, 3, 7)] == 5 * 1200 + 1440  # overlapping a little: counted once
    shows = {"s1": ("Frieren", "s1"), "s2": ("Hacks", "s2")}
    slides = build_slides("me", "Sam", stats, infos, 2026, shows, {"m": "Dune"})
    by_id = {s["id"]: s for s in slides}
    assert by_id["total"]["value"] == "7.4" and by_id["total"]["unit"] == "hours watched"
    assert by_id["crown"]["title"] == "You're the server's number one anime watcher"
    assert by_id["show"]["title"] == "Frieren" and by_id["show"]["caption"] == "6 episodes this year."
    assert by_id["movie"]["value"] == "2" and by_id["movie"]["unit"] == "times"
    assert by_id["binge"]["value"] == "6"
    assert by_id["rank"]["title"] == "You watched more than anyone on the server"
    assert slides[-1]["id"] == "outro"
    assert all(v not in (None, "", []) for s in slides for v in s.values())


def test_helpers():
    assert hours(45 * 60) == ("45", "minutes") and hours(3600) == ("1", "hour") and hours(5 * 3600 + 1800) == ("5.5", "hours")
    assert hours(1234 * 3600) == ("1,234", "hours")
    assert genres_of(["Sci-Fi & Fantasy", "Fantasy", "Drama"]) == ["Science Fiction", "Fantasy", "Drama"]
    assert longest_streak([date(2026, 1, d) for d in (1, 2, 3, 5, 6)]) == (3, date(2026, 1, 3))


def test_window():
    w = Wrapped.__new__(Wrapped)
    w.opens = "12-01"
    assert w.window(datetime(2026, 11, 26)) == (date(2026, 12, 1), date(2027, 1, 1))


def test_gone_channels():
    for name in ("US| ESPN 2 HD", "US - FOX 5 NEW YORK NY (WNYW) HD", "4K| SKY SPORTS F1 UHD",
                 "MLB 7 | New york yankees @ philadelphia phillies"):
        assert looks_like_channel(name), name
    for name in ("FBI - Most Wanted Man", "Loot - Joyride", "Mission: Impossible - Fallout"):
        assert not looks_like_channel(name), name
