from datetime import datetime
from zoneinfo import ZoneInfo

from app.events import looks_like_event, parse
from app.names import tidy


def test_spaces_separators_and_echoes():
    assert tidy("PBS NJ | NEW JERSEY (WNJT)") == "PBS NJ · New Jersey (WNJT)"
    assert tidy("UFC| 24/7") == "UFC 24/7"
    assert tidy("WWE: WWE NXT (WWE NXT)") == "WWE NXT"
    assert tidy("NHL: NYI vs. NYR • WWE NXT (NYI vs. NYR )") == "NHL: NYI vs. NYR • WWE NXT"
    assert tidy("WWE: WWE Money in the Bank Special (WWE Money in the Bank Special)") == "WWE Money in the Bank Special"


def test_shouting_names_recased_with_brands_and_call_signs():
    assert tidy("CBS 2 NEW YORK NY (WCBS)") == "CBS 2 New York NY (WCBS)"
    assert tidy("MYTV 9 NEW YORK (WWOR)") == "MyTV 9 New York (WWOR)"
    assert tidy("ISLANDERS @ RANGERS") == "Islanders @ Rangers"
    assert tidy("EN - MANCHESTER CITY VS REAL MADRID") == "Manchester City vs Real Madrid"
    assert tidy("BEIN SPORTS") == "beIN Sports"
    assert tidy("NFL REDZONE") == "NFL RedZone"
    assert tidy("SPORTV 2") == "SporTV 2"
    assert tidy("WE TV") == "WE tv"
    assert tidy("MS NOW") == "MS NOW"
    assert tidy("SÃO PAULO: RACE") == "São Paulo: Race"
    assert tidy("ESPN SEC NETWORK") == "ESPN SEC Network"


def test_names_already_written_properly_are_kept():
    assert tidy("Crystal Palace vs Lech Poznań") == "Crystal Palace vs Lech Poznań"
    assert tidy("UFC Fight Night Preview Show: Allen vs. Duncan") == "UFC Fight Night Preview Show: Allen vs. Duncan"


def test_european_clock_slots_get_a_start_time():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("UTC"))
    name = "EN - PSG VS OH LEUVEN — 19:00 CET"
    assert looks_like_event(name)
    event = parse(name, now)
    assert event.start.isoformat() == "2026-10-07T19:00:00+02:00"
    assert tidy(event.title) == "PSG vs OH Leuven"


def test_f1_tv_feeds_and_onboards():
    from app.events import clean_title
    assert tidy(clean_title("F1 - DRIVER - VERSTAPPEN [RED BULL] FHD")) == "Onboard · Verstappen (Red Bull)"
    assert tidy(clean_title("F1 - DRIVER - TSUNODA [RB] FHD")) == "Onboard · Tsunoda (RB)"
    assert tidy(clean_title("F1 - F1 TV - RACE DATA FEED FHD")) == "F1 TV · Race Data Feed"
    assert tidy(clean_title("F1 - F1 TV - UHD FEED FHD")) == "F1 TV · 4K Feed"


def test_apple_f1_slot():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.events import clean_title, looks_like_event, parse
    name = "US - APPLE F1 03 : SINGAPORE GP: RACE OCT 11 \u2013 8:00 AM ET"
    assert looks_like_event(name)
    event = parse(name, datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("UTC")))
    assert event.start is not None and event.start.month == 10 and event.start.day == 11
    assert tidy(event.title) == "Singapore GP: Race"


def test_provider_mojibake_is_repaired():
    assert tidy("SÃƒO PAULO: RACE") == "São Paulo: Race"  # "SÃƒO" (UTF-8 read as cp1252 twice)
    assert tidy("SÃO PAULO: RACE") == "São Paulo: Race"  # already right
    assert tidy("Ã‰cole") == "École"
    assert tidy("Crystal Palace vs Lech Poznań") == "Crystal Palace vs Lech Poznań"


def test_guide_text_repaired_run_by_run():
    from app.names import repair
    assert repair("doesnâ€™t stop — JosÃ©") == "doesn’t stop — José"
    assert repair("NEW JERSEY") == "NEW JERSEY"
    assert repair("Plzeň · Ærø Ø") == "Plzeň · Ærø Ø"
