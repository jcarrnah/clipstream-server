from app.backups import key, ordered, search_term

PRIORITY = {5: 3, 3: 2, 2: 1}  # account id -> priority


def stream(id, name, account, stream_id=None):
    return {"id": id, "name": name, "m3u_account": account, "stream_id": stream_id}


def test_same_channel_across_naming_styles():
    assert key("US - ESPN HD ◉", alias=True) == key("US| ESPN ᴴᴰ ⁶⁰ᶠᵖˢ") == ("US", "ESPN")
    assert key("US - SPECTRUM NEWS NY1 HD") == key("US| SPECTRUM NEWS - NY1")
    assert key("BR - GLOBO SP FHD") == key("BR| GLOBO SP HD")
    assert key("US - ESPN 2 HD") != key("US| ESPN HD")
    assert key("US - TNT EAST HD ◉") == key("US| TNT")
    # Another country's feed of the same network isn't the same channel.
    assert key("US - ESPN HD") != key("BR| ESPN HD")
    assert key("LAT - TELEMUNDO 47 (NEW YORK) HD", alias=True)[0] == "US"
    assert key("LAT| ESPN")[0] == "LAT"


def test_order_follows_account_priority():
    own = stream(1, "US - ESPN HD ◉", 5, "71936")
    candidates = [stream(11, "US| ESPN ᴴᴰ ⁶⁰ᶠᵖˢ", 2), stream(9, "US| ESPN HD", 2, "111"), stream(7, "US - ESPN HD ◉", 3, "71936"),
                  stream(8, "US - ESPN 2 HD ◉", 3, "71937"), stream(10, "LAT| ESPN", 2)]
    assert ordered(own, candidates, PRIORITY) == [1, 7, 9]


def test_creator_from_a_lower_priority_account_moves_behind_better_ones():
    own = stream(1, "US| NJ PBS (WNJT)", 2)
    assert ordered(own, [stream(4, "US - NJ PBS (WNJT) HD", 5)], PRIORITY) == [4, 1]


def test_duplicate_names_pick_the_same_provider_stream():
    own = stream(1, "US - NHL TEAMS - NEW YORK RANGERS ", 5, "576221")
    candidates = [stream(20, "US - NHL TEAMS - NEW YORK RANGERS ", 3, "1044046"),
                  stream(21, "US - NHL TEAMS - NEW YORK RANGERS ", 3, "576221")]
    assert ordered(own, candidates, PRIORITY) == [1, 21]


def test_renamed_slot_drops_the_stale_backup():
    own = stream(1, "US - NFL GAME 03 : JETS @ BILLS", 5, "500")
    assert ordered(own, [stream(2, "US - NFL GAME 03 : PATRIOTS @ DOLPHINS", 3, "500")], PRIORITY) == [1]


def test_search_term_is_the_longest_word():
    assert search_term("US - SPECTRUM NEWS NY1 HD ◉") == "SPECTRUM"
    assert search_term("US - FX EAST HD ◉") == "FX"
    assert search_term("US - A&E EAST HD") == "A&E"
    assert search_term("US - E! EAST HD ◉") == "E!"


def test_lat_channels_match_lat_or_us_copies():
    own = stream(1, "LAT - TELEMUNDO 47 (NEW YORK) HD", 5, "9")
    candidates = [stream(2, "LAT - TELEMUNDO 47 (NEW YORK) HD", 3, "9"), stream(3, "US| TELEMUNDO 47 (NEW YORK)", 2)]
    assert ordered(own, candidates, PRIORITY) == [1, 2, 3]
