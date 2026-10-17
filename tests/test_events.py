from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.events import clean_title, looks_like_event, parse

ET = ZoneInfo("America/New_York")
# Thursday Sep 24 2026, 6pm Eastern.
NOW = datetime(2026, 9, 24, 18, 0, tzinfo=ET)


def test_start_stop_utc_and_capped_end():
    e = parse("MLB 08 : Rays x Yankees start:2026-09-25 00:05:00 stop:2026-09-25 07:18:20", NOW)
    assert e.title == "Rays x Yankees"
    assert e.start == datetime(2026, 9, 25, 0, 5, tzinfo=timezone.utc)
    assert (e.end - e.start).total_seconds() == 4 * 3600  # padded stop time capped
    assert e.status == "upcoming"


def test_time_only_is_today_local():
    e = parse("NHL | 04 - 7pm Devils at Rangers", NOW)
    assert e.title == "Devils at Rangers"
    assert e.start == datetime(2026, 9, 24, 19, 0, tzinfo=ET)
    assert e.status == "upcoming"
    assert parse("NFL  | 07 - 1pm Titans at Giants", NOW).title == "Titans at Giants"


def test_streaming_service_listing_is_eastern():
    e = parse("US (Paramount 029) | UFC 332: Multiview (2026-10-03 20:00:00)", NOW)
    assert e.title == "UFC 332: Multiview"
    assert e.start == datetime(2026, 10, 3, 20, 0, tzinfo=ET)


def test_next_prefix_weekday_date_with_zone():
    e = parse("NEXT | SINGAPORE: RACE | Sun 11 Oct 06:50 EDT (US) | 8K EXCLUSIVE | US: APPLE TV F1 PPV 4", NOW)
    assert e.title == "SINGAPORE: RACE"
    assert e.start == datetime(2026, 10, 11, 6, 50, tzinfo=ET)
    assert e.status == "upcoming"


def test_gmt_listing_and_explicit_end_status():
    e = parse("End | Cejudo vs. Walton: Press Conference | Misfits Boxing | 2026-09-24 | 21:00 (GMT) | 8K EXCLUSIVE | US: DAZN PPV 9", NOW)
    assert e.status == "ended"
    assert e.start == datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)
    assert e.title.startswith("Cejudo vs. Walton")


def test_stale_summer_league_is_ended():
    e = parse("NBA Summer League Warriors vs. Knicks jul 16 :NBA 04", NOW)
    assert e.status == "ended"
    assert "Warriors vs. Knicks" in e.title


def test_game_slot_package():
    e = parse("US - MLB GAME 08 : RAYS @ YANKEES SEP 25 – 12:05 AM ET / 5:05 AM UK", NOW)
    assert e.title == "RAYS @ YANKEES"
    assert e.start == datetime(2026, 9, 25, 0, 5, tzinfo=ET)
    assert e.status == "upcoming"
    assert looks_like_event("US - NFL GAME 07 : TITANS @ GIANTS SEP 27 – 1:00 PM ET / 6:00 PM UK")
    assert parse("US - NHL GAME 04 : DEVILS @ RANGERS SEP 24 – 7:00 PM ET / 12:00 AM UK", NOW).status == "upcoming"
    assert clean_title("US - NFL TEAMS - JETS ") == "JETS"
    assert clean_title("US - MLB TEAMS : NEW YORK YANKEES ") == "NEW YORK YANKEES"
    assert not looks_like_event("US - YES NETWORK HD ◉")


def test_undated_event_is_unknown():
    e = parse("Boxing 8: Jake Paul vs Anthony Joshua", NOW)
    assert e.status == "unknown"
    assert e.title == "Jake Paul vs Anthony Joshua"


def test_regular_channels_are_not_events_and_get_clean_names():
    assert not looks_like_event("US| NBC 4 NEW YORK NY (WNBC)")
    assert not looks_like_event("US| YES NETWORK HD")
    assert clean_title("US| YES NETWORK HD") == "YES NETWORK"
    assert clean_title("BR - GLOBO SP FHD") == "GLOBO SP"
    assert looks_like_event("NHL | 04 - 7pm Devils at Rangers")
    assert looks_like_event("NBA Summer League Nets vs. Rockets jul 16 :NBA 02")
    assert not looks_like_event("US| MSG 2 PLUS HD")
    assert not looks_like_event("US| MLB STRIKE ZONE HD")
    assert looks_like_event("US (ESPN+ 487) | Pro Wrestling: Money in the Bank 2026 (2026-10-10 16:00:00)")
