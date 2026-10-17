"""Display names for channels and events: what's left after events.clean_title() strips provider noise, made to read
like a TV guide. "PBS NJ | NEW JERSEY (WNJT)" -> "PBS NJ · New Jersey (WNJT)", "WWE: WWE NXT (WWE NXT)" -> "WWE NXT",
"UFC| 24/7" -> "UFC 24/7", "MANCHESTER CITY VS REAL MADRID" -> "Manchester City vs Real Madrid".

Capitalization only changes when a name is (nearly) all caps, so names a provider already wrote properly are kept."""
from __future__ import annotations

import re
import unicodedata

# Kept upper case when a shouting name is recased: networks, leagues, US states, and other initialisms.
ACRONYMS = {
    # networks and services
    "ABC", "CBS", "NBC", "FOX", "PBS", "CNN", "CNBC", "MSNBC", "ESPN", "ESPN2", "ESPNU", "HBO", "MSG", "SNY", "YES", "TNT",
    "TBS", "AMC", "FX", "FXX", "USA", "BBC", "ITV", "TSN", "CW", "MTV", "VH1", "BET", "TLC", "HGTV", "CNNI", "NHK",
    "BTN", "SEC", "ACC", "CBSSN", "NESN", "NFLN", "MLBN", "NBATV", "NHLN", "WWE", "AEW", "UFC", "PPV", "TV", "HD", "UHD",
    "DAZN", "BEIN", "TUDN", "TYC", "ESPNEWS", "PIX11", "NY1", "C-SPAN", "CSPAN", "MLB", "NFL", "NBA", "NHL", "MLS", "WNBA",
    "NCAA", "NCAAF", "NCAAB", "UEFA", "FIFA", "EPL", "F1", "F2", "F3", "IMSA", "NASCAR", "PGA", "LIV", "ATP", "WTA",
    "NXT", "PSG", "AC", "FC", "SC", "CF", "AFC", "SG", "UK", "US", "EU", "NJ", "NY", "CT", "PA", "LA", "SF", "DC", "II",
    "III", "IV", "SP", "RJ", "BR", "TNF", "MNF", "SNF", "NYI", "NYR", "4K", "8K", "24/7",
}
ACRONYMS |= {"RB", "GP", "4K", "NBCSN", "CBSN", "TNA", "MS", "NOW", "OH", "CET", "CEST", "ET", "EST", "EDT", "PT", "GMT", "BST", "UTC", "RTL"}
# Brands with their own capitalization.
BRANDS = {"BEIN": "beIN", "SPORTV": "SporTV", "REDZONE": "RedZone", "STRIKEZONE": "StrikeZone", "MYTV": "MyTV", "MCLAREN": "McLaren", "A&E": "A&E", "NEWSNATION": "NewsNation", "TRUTV": "truTV",
          "WE": "WE", "E!": "E!", "IFC": "IFC", "CMT": "CMT", "OWN": "OWN", "TV": "TV"}
# "WE TV" is the network's own styling; "MS NOW" too.
PHRASES = {"WE TV": "WE tv", "C-SPAN": "C-SPAN", "TRU TV EAST": "truTV East", "TRU TV": "truTV", "MS NOW": "MS NOW", "TV LAND": "TV Land", "HISTORY": "History"}
# Lower case inside a recased name (not as the first word).
SMALL = {"vs", "vs.", "at", "of", "the", "and", "in", "on", "de", "da", "do", "del", "la", "le", "x", "a", "for", "to"}


def tidy(name: str) -> str:
    """The display name for a channel or event title."""
    text = unicodedata.normalize("NFC", _unmangle(name))
    # Non-breaking and other unusual spaces (providers use \\u00a0 between words) -> plain spaces.
    text = "".join(" " if unicodedata.category(c) == "Zs" or c in "\t\r\n" else c for c in text)
    # A provider's language/market tag in front: "EN - ", "LAT - ".
    text = re.sub(r"^(EN|ES|FR|DE|IT|PT|LAT|LATAM)\s*-\s+", "", text, flags=re.I)
    # Separators: "UFC| 24/7" -> "UFC 24/7"; "PBS NJ | NEW JERSEY" -> "PBS NJ · NEW JERSEY".
    text = re.sub(r"^([A-Za-z0-9]+)\|\s*", r"\1 ", text)
    text = re.sub(r"\s+\|\s+", " · ", text)
    text = re.sub(r"\s*\|\s*", " ", text)
    # "( USM vs. TROY )" -> "(USM vs. TROY)"
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = _drop_echo(text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -:·")
    if _shouting(text):
        text = PHRASES.get(text.upper(), None) or _recase(text)
    # F1 TV channels as some providers name them (after recasing): "F1 - Driver - Verstappen [Red Bull]", "F1 - F1 TV - Race Feed".
    text = re.sub(r"^F1 - DRIVER - (.+?)\s*\[(.+?)\]\s*$", r"Onboard · \1 (\2)", text, flags=re.I)
    text = re.sub(r"^F1 - (F1 TV) - ", r"\1 · ", text, flags=re.I)
    return text or name.strip()


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _drop_echo(text: str) -> str:
    """Repeated words: "WWE: WWE NXT (WWE NXT)" -> "WWE NXT"; "NHL: NYI vs. NYR (NYI vs. NYR)" -> "NHL: NYI vs. NYR"."""
    # A trailing "(...)" that only repeats what's already in the name.
    m = re.search(r"\s*\(([^()]*)\)\s*$", text)
    if m and _key(m.group(1)) and _key(m.group(1)) in _key(text[:m.start()]):
        text = text[:m.start()]
    # "WWE: WWE Money in the Bank" -> "WWE Money in the Bank"
    m = re.match(r"^([A-Za-z0-9]+):\s*(.+)$", text)
    if m and _key(m.group(2)).startswith(_key(m.group(1))):
        text = m.group(2)
    return text


def _shouting(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.8


def _recase(text: str) -> str:
    words = re.split(r"(\s+|[()·:/,-])", text)
    out = []
    first = True
    for i, w in enumerate(words):
        if not w or not w.strip() or re.fullmatch(r"[()·:/,-]", w):
            out.append(w)
            if w in ("(", ":", "·", "-"):
                first = True
            continue
        bare = w.strip(".[]")
        if bare.upper() in BRANDS and bare.upper() not in ("WE",):
            out.append(w.replace(bare, BRANDS[bare.upper()]))
            first = False
            continue
        inside_parens = i > 0 and words[i - 1] == "(" and i + 1 < len(words) and words[i + 1] == ")"
        if bare.upper() in ACRONYMS or any(c.isdigit() for c in w):
            out.append(w.upper() if bare.upper() in ACRONYMS else w)
        elif inside_parens and re.fullmatch(r"[KW][A-Z]{2,4}(-[A-Z]{2})?", w):
            out.append(w)  # a call sign: "(WNJT)", "(WCBS)"
        elif not first and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(_cap(w))
        first = False
    return "".join(out)


def _cap(word: str) -> str:
    """"YORK" -> "York", "O'NEIL" -> "O'Neil", "[RED" -> "[Red"; "MCDONALD" stays simple ("Mcdonald"): good enough for a guide."""
    def one(p: str) -> str:
        lead = len(p) - len(p.lstrip("[\"'("))
        return p[:lead] + p[lead:lead + 1].upper() + p[lead + 1:].lower()
    return "'".join(one(p) for p in word.split("'"))


# UTF-8 that was read as Windows-1252 (once or twice) on the provider's side: "SÃƒO PAULO", "Ã©", "doesnâ€™t".
# A mangled character is a UTF-8 lead byte (as its cp1252 character) followed by 1-3 continuation bytes (0x80-0xBF).
def _cp1252_char(b: int) -> str:
    try:
        return bytes([b]).decode("cp1252")
    except UnicodeDecodeError:
        return chr(b)  # bytes cp1252 leaves undefined come through as the same code point


_CONT = "".join(re.escape(_cp1252_char(b)) for b in range(0x80, 0xC0))
_LEAD = "".join(re.escape(_cp1252_char(b)) for b in range(0xC2, 0xF5))
_MANGLED = re.compile(f"[{_LEAD}][{_CONT}]{{1,3}}")


def _fix_run(m: re.Match) -> str:
    run = m.group(0)
    try:
        raw = bytes(ord(c) if ord(c) < 256 and not (0x80 <= ord(c) < 0xA0) else c.encode("cp1252")[0] for c in run)
        return raw.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
        return run


def _unmangle(text: str) -> str:
    """Undoes UTF-8 -> Windows-1252 mojibake run by run (twice, for double encoding); runs that don't decode as UTF-8
    are real text and stay as they are."""
    for _ in range(2):
        fixed = _MANGLED.sub(_fix_run, text)
        if fixed == text:
            break
        text = fixed
    return text


def repair(text: str | None) -> str | None:
    """Guide text (program titles, descriptions): mojibake undone, non-breaking spaces made plain; otherwise as written."""
    if not text:
        return text
    return re.sub(r"[   ]", " ", _unmangle(text))
