from datetime import datetime, timedelta, timezone

from app.events import Event, parse
from app.scores import Game, Match, apply, find, parse_scoreboard

UTC = timezone.utc
NOW = datetime(2026, 10, 31, 0, 20, tzinfo=UTC)  # Oct 30, 8:20 PM Eastern
PRE, POST = timedelta(minutes=30), timedelta(minutes=30)


def team(display, short, location, name, abbr):
    return {"team": {"displayName": display, "shortDisplayName": short, "location": location, "name": name,
                     "abbreviation": abbr}}


def event(id, date, state, home, away, status_name="STATUS_IN_PROGRESS"):
    return {"id": id, "date": date, "status": {"type": {"state": state, "name": status_name}},
            "competitions": [{"competitors": [home, away]}]}


DEVILS = team("New Jersey Devils", "Devils", "New Jersey", "Devils", "NJ")
VEGAS = team("Vegas Golden Knights", "Golden Knights", "Vegas", "Golden Knights", "VGK")
KNICKS = team("New York Knicks", "Knicks", "New York", "Knicks", "NY")
SIXERS = team("Philadelphia 76ers", "76ers", "Philadelphia", "76ers", "PHI")
LAKERS = team("Los Angeles Lakers", "Lakers", "Los Angeles", "Lakers", "LAL")
WARRIORS = team("Golden State Warriors", "Warriors", "Golden State", "Warriors", "GS")
IOWA = team("Iowa Hawkeyes", "Iowa", "Iowa", "Hawkeyes", "IOWA")
WISC = team("Wisconsin Badgers", "Wisconsin", "Wisconsin", "Badgers", "WIS")

GAMES = (parse_scoreboard("NHL", {"events": [event("1", "2026-10-30T22:00Z", "in", VEGAS, DEVILS)]})
         + parse_scoreboard("NBA", {"events": [
             event("2", "2026-10-30T23:30Z", "in", SIXERS, KNICKS),
             event("3", "2026-10-31T02:00Z", "pre", WARRIORS, LAKERS, "STATUS_SCHEDULED")]})
         + parse_scoreboard("NCAAF", {"events": [event("4", "2026-10-31T16:00Z", "pre", IOWA, WISC, "STATUS_SCHEDULED")]}))
LOADED = {"NHL", "NBA", "NCAAF"}


def slot(name):
    e = parse(name, NOW)
    return e, find(name, e.title, e.start, GAMES, LOADED, NOW)


def test_matches_both_teams_whatever_the_order():
    _, m = slot("US - NHL GAME 01 : NEW JERSEY DEVILS @ VEGAS GOLDEN KNIGHTS OCT 30 – 6:00 PM ET / 10:00 PM UK")
    assert m.game and m.game.id == "1"
    _, m = slot("US - NBA GAME 04 : PHILADELPHIA 76ERS @ NEW YORK KNICKS OCT 30 – 7:30 PM ET")
    assert m.game and m.game.id == "2"


def test_abbreviations_match():
    _, m = slot("NBA 05 : LAL x GS")
    assert m.game and m.game.id == "3"


def test_one_team_with_a_typo_matches_near_its_time():
    _, m = slot("NCAAF 34: Wisconsin at lowa 12pm")  # Oct 31 noon would be tomorrow; listed as today
    assert m.game is None  # 12pm today is a day off from tomorrow's game
    e = Event(title="Wisconsin at lowa", start=datetime(2026, 10, 31, 16, 0, tzinfo=UTC))
    m = find("NCAAF 34: Wisconsin at lowa", e.title, e.start, GAMES, LOADED, NOW)
    assert m.game and m.game.id == "4"


CHARLOTTE = team("Charlotte 49ers", "Charlotte", "Charlotte", "49ers", "CLT")
TULANE = team("Tulane Green Wave", "Tulane", "Tulane", "Green Wave", "TULN")
UNC = team("North Carolina Tar Heels", "North Carolina", "North Carolina", "Tar Heels", "UNC")
SOUTH_ALABAMA = team("South Alabama Jaguars", "South Alabama", "South Alabama", "Jaguars", "USA")
MORE = parse_scoreboard("NCAAF", {"events": [
    event("5", "2026-10-30T23:00Z", "in", CHARLOTTE, TULANE),
    event("6", "2026-10-31T17:00Z", "pre", UNC, SOUTH_ALABAMA, "STATUS_SCHEDULED")]})


def test_one_team_match_needs_the_other_name_to_fit():
    e = parse("NCAAF 58: 7PM North Carolina at Charlotte", NOW)
    m = find("NCAAF 58: 7PM North Carolina at Charlotte", e.title, e.start, GAMES + MORE, LOADED, NOW)
    assert m.game is None and not m.missing  # unsure: keep the provider's times
    e = parse("NCAAF 16: USA NETWORK", NOW)
    m = find("NCAAF 16: USA NETWORK", e.title, e.start, GAMES + MORE, LOADED, NOW)
    assert m.game is None and not m.missing
    assert apply(e, m, NOW, PRE, POST)


def test_leftover_slot_not_on_the_schedule_is_hidden():
    e, m = slot("NCAAF 01: NC State at Wake Forest 7:30pm")
    assert m.missing
    assert not apply(e, m, NOW, PRE, POST)


def test_team_playing_another_day_does_not_save_a_leftover():
    e, m = slot("NCAAF 02: Wisconsin at Rutgers 7pm")  # Wisconsin plays Iowa tomorrow
    assert m.missing


def test_no_matchup_or_league_not_loaded_is_not_a_leftover():
    _, m = slot("US - NHL GAME 09 : OCT 30 – 7:00 PM ET")
    assert not m.missing
    e = parse("MLB 08 : Rays x Yankees 7pm", NOW)
    assert not find("MLB 08 : Rays x Yankees 7pm", e.title, e.start, GAMES, LOADED, NOW).missing


def test_in_progress_shows_past_the_listed_end():
    e, m = slot("US - NHL GAME 01 : NEW JERSEY DEVILS @ VEGAS GOLDEN KNIGHTS OCT 30 – 6:00 PM ET / 10:00 PM UK")
    late = NOW + timedelta(hours=2)  # 4h20 after the start: overtime, shootout
    assert apply(e, m, late, PRE, POST)
    assert e.status == "live" and e.end > late


def test_pregame_window_uses_the_real_start():
    g = Game("x", "NBA", NOW + timedelta(minutes=45), "pre", [])
    e = Event(title="Lakers at Warriors", start=NOW - timedelta(hours=2), status="live")  # provider time wrong
    assert not apply(e, Match(g), NOW, PRE, POST)
    assert apply(e, Match(g), NOW + timedelta(minutes=20), PRE, POST)
    assert e.status == "upcoming" and e.start == g.start
    assert apply(e, Match(g), NOW + timedelta(minutes=50), PRE, POST)  # start passed, not begun: a delay
    assert e.status == "live"


def test_postgame_window_then_hidden():
    g = Game("x", "NHL", NOW - timedelta(hours=3), "post", [], ended_at=NOW)
    e = Event(title="a at b")
    assert apply(e, Match(g), NOW + timedelta(minutes=25), PRE, POST)
    assert not apply(e, Match(g), NOW + timedelta(minutes=35), PRE, POST)


def test_postponed_is_hidden():
    g = parse_scoreboard("MLB", {"events": [event("9", "2026-10-31T00:00Z", "pre", IOWA, WISC, "STATUS_POSTPONED")]})[0]
    assert not apply(Event(title="x"), Match(g), NOW, PRE, POST)


def test_unmatched_events_use_provider_times_with_leeway():
    later = Event(title="UFC 330", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=5), status="upcoming")
    assert not apply(later, Match(), NOW, PRE, POST)
    soon = Event(title="UFC 330", start=NOW + timedelta(minutes=20), status="upcoming")
    assert apply(soon, Match(), NOW, PRE, POST)
    over = Event(title="UFC 330", start=NOW - timedelta(hours=3, minutes=30), end=NOW - timedelta(minutes=30), status="ended")
    assert apply(over, Match(), NOW, PRE, POST) and over.status == "live"
    gone = Event(title="UFC 330", start=NOW - timedelta(hours=5), end=NOW - timedelta(hours=2), status="ended")
    assert not apply(gone, Match(), NOW, PRE, POST)
    assert not apply(Event(title="NEXT", status="upcoming"), Match(), NOW, PRE, POST)  # "NEXT |" with no time
    assert apply(Event(title="whenever", status="unknown"), Match(), NOW, PRE, POST)
