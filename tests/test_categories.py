from app.categories import load


def cats():
    return {c.id: c for c in load()}


def test_msg_is_in_basketball_and_hockey_but_not_baseball():
    c = cats()
    assert c["basketball"].matches("Sports Networks", "US| MSG HD")
    assert c["hockey"].matches("Sports Networks", "US| MSG 2 PLUS HD")
    assert not c["baseball"].matches("Sports Networks", "US| MSG HD")


def test_espn_is_regular_tv_and_uefa_slots_are_soccer_not_events():
    c = cats()
    assert c["news-tv"].matches("Sports Networks", "US| ESPN 2 HD")
    assert c["soccer"].matches("Events", "UEFA | 09 - Plzeň vs R. Union SG 8:00 pm")
    assert not c["events"].matches("Events", "UEFA | 09 - Plzeň vs R. Union SG 8:00 pm")


def test_favorite_teams_are_word_matches_per_sport():
    c = cats()
    assert c["football"].featured("Titans at Giants")
    assert c["baseball"].featured("Rays x Yankees")
    assert not c["baseball"].featured("Giants x Dodgers")  # SF Giants aren't a baseball favorite
