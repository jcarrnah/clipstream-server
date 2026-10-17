"""Reads event slot names ("MLB 08 : Rays x Yankees start:2026-09-25 00:05:00 stop:...", "NFL | 07 - 1pm Titans at
Giants", "US (Paramount 029) | UFC 332: Multiview (2026-10-03 20:00:00)", "NEXT | SINGAPORE: RACE | Sun 11 Oct 06:50
EDT (US) | 8K EXCLUSIVE | US: APPLE TV F1 PPV 4") into a clean title plus start/end/status.

Providers rename these slots every few hours, so this is best effort: anything we can't date is kept with an
unknown time rather than dropped."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ZONES = {"EDT": "America/New_York", "EST": "America/New_York", "ET": "America/New_York",
         "CDT": "America/Chicago", "CST": "America/Chicago", "PDT": "America/Los_Angeles", "PST": "America/Los_Angeles",
         "UTC": "UTC", "GMT": "UTC", "BST": "Europe/London", "UK": "Europe/London",
         "CET": "Europe/Paris", "CEST": "Europe/Paris"}
# Sports events without an end time are assumed to run this long.
DEFAULT_LENGTH = timedelta(hours=3)


@dataclass
class Event:
    title: str
    start: datetime | None = None
    end: datetime | None = None
    status: str = "unknown"  # upcoming | live | ended | unknown
    time_only: bool = False  # only a clock time was given ("7pm"), assumed to be today

    def as_dict(self) -> dict:
        return {"title": self.title, "start": _iso(self.start), "end": _iso(self.end), "status": self.status}


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


# Recognizes a slot at all: numbered package slots, provider-prefixed listings, "Live | ... ", "(2026-..)", "start:".
SLOT = re.compile(
    r"start:\d{4}-|\(\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)?\)|^\s*(next|live|end|upcoming)\s*\|"
    r"|^(MLB|NFL|NBA|NHL|NCAAF|NCAAB|UFC|UEFA|Boxing|LIVE EVENT|Pay Per View)\s*\d*\s*[:|-]"
    r"|^US \((Paramount|ESPN\+|Peacock)[^)]*\)\s*\||\d{1,2}(:\d\d)?\s*(am|pm)\b|\b\d{1,2}-\d{1,2}-\d{4}\b"
    r"|:\s*(MLB|NBA|MLS|NFL|NHL)\s*\d+\s*$"  # "... jul 16 :NBA 04"
    r"|([—–-]\s*|\s)\d{1,2}:\d\d\s*(CET|CEST|ET|EST|EDT|GMT|BST|UK|UTC)\s*$"  # "EN - PSG VS OH LEUVEN — 19:00 CET", "... VS LILLE 20:00 CET"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b(?!\s*(HD|FHD|\d))",  # "Sep 23", "jul 16"
    re.I,
)


def looks_like_event(name: str) -> bool:
    return bool(SLOT.search(name))


def parse(name: str, now: datetime, local_tz: str = "America/New_York") -> Event:
    """Best-effort event info from a slot name. `now` must be timezone-aware."""
    local = ZoneInfo(local_tz)
    start = end = None
    explicit_status = None
    text = name.strip()

    m = re.match(r"^\s*(next|live|end|ended|upcoming)\s*\|\s*", text, re.I)
    if m:
        explicit_status = {"next": "upcoming", "upcoming": "upcoming", "live": "live", "end": "ended", "ended": "ended"}[m.group(1).lower()]
        text = text[m.end():]

    # start:YYYY-MM-DD HH:MM:SS stop:YYYY-MM-DD HH:MM:SS (UTC)
    m = re.search(r"start:(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*(stop:(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d))?", text)
    if m:
        start = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
        if m.group(3):
            end = datetime.fromisoformat(m.group(3)).replace(tzinfo=UTC)
            # These "stop" times are padded by hours; cap at a normal event length.
            end = min(end, start + DEFAULT_LENGTH + timedelta(hours=1))
        text = text[:m.start()] + text[m.end():]

    # (YYYY-MM-DD HH:MM[:SS]): US streaming-service listings, in US Eastern time
    if start is None:
        m = re.search(r"\((\d{4}-\d\d-\d\d) (\d\d:\d\d)(:\d\d)?\)", text)
        if m:
            start = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").replace(tzinfo=local)
            text = text[:m.start()] + text[m.end():]

    # | YYYY-MM-DD | HH:MM (GMT) |  or  | DD-MM-YYYY | HH:MM (GMT) |
    if start is None:
        m = re.search(r"(\d{4}-\d\d-\d\d|\d\d-\d\d-\d{4})\s*\|\s*(\d\d:\d\d)\s*\((GMT|UTC)\)", text)
        if m:
            d = m.group(1)
            day = date.fromisoformat(d) if d[4] == "-" else date(int(d[6:]), int(d[3:5]), int(d[:2]))
            start = datetime.combine(day, time.fromisoformat(m.group(2)), UTC)

    # Sun 25 Oct 14:50 EDT   /   Sat 09 Aug 15:45
    if start is None:
        m = re.search(r"\b(?:mon|tue|wed|thu|fri|sat|sun)\w*\s+(\d{1,2})\s+([a-z]{3})\w*\s+(\d{1,2}):(\d\d)(?:\s+([A-Z]{2,4})\b)?", text, re.I)
        if m and m.group(2).lower() in MONTHS:
            tz = ZoneInfo(ZONES.get((m.group(5) or "").upper(), local_tz))
            start = _nearest_year(now, MONTHS[m.group(2).lower()], int(m.group(1)), int(m.group(3)), int(m.group(4)), tz)

    # Sep 23 9:30 PM  /  jul 16  /  SEP 25 – 12:05 AM ET
    if start is None:
        # First word+number whose word is a month ("GAME 08" comes before "SEP 25" in some providers' names).
        m = next((x for x in re.finditer(r"\b([a-z]{3})\w*\s+(\d{1,2})(?:\s*[–-]?\s*(\d{1,2})(?::(\d\d))?\s*(am|pm))?\b", text, re.I)
                  if x.group(1).lower() in MONTHS), None)
        if m:
            hour, minute = _clock(m.group(3), m.group(4), m.group(5)) if m.group(3) else (12, 0)
            start = _nearest_year(now, MONTHS[m.group(1).lower()], int(m.group(2)), hour, minute, local)

    # "— 19:00 CET" at the end, no date: today, in that zone
    time_only = False
    if start is None:
        m = re.search(r"(?:[—–-]\s*|\s)(\d{1,2}):(\d\d)\s*(CET|CEST|ET|EST|EDT|GMT|BST|UK|UTC)\s*$", text, re.I)
        if m:
            tz = ZoneInfo(ZONES[m.group(3).upper()])
            start = datetime.combine(now.astimezone(tz).date(), time(int(m.group(1)), int(m.group(2))), tz)
            time_only = True

    # 7pm / 8:20pm / 8:00 pm with no date: today, local time
    if start is None:
        m = re.search(r"\b(\d{1,2})(?::(\d\d))?\s*(am|pm)\b", text, re.I)
        if m:
            hour, minute = _clock(m.group(1), m.group(2), m.group(3))
            start = datetime.combine(now.astimezone(local).date(), time(hour, minute), local)
            time_only = True

    if start is not None and end is None:
        end = start + DEFAULT_LENGTH

    status = explicit_status or _status(start, end, now)
    return Event(title=clean_title(name), start=start, end=end, status=status, time_only=time_only)


def _clock(hour: str, minute: str | None, ampm: str) -> tuple[int, int]:
    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    return h, int(minute or 0)


def _nearest_year(now: datetime, month: int, day: int, hour: int, minute: int, tz: ZoneInfo) -> datetime | None:
    """Listings omit the year: pick the one that puts the date closest to now."""
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute, tzinfo=tz))
        except ValueError:
            continue
    return min(candidates, key=lambda d: abs(d - now)) if candidates else None


def _status(start: datetime | None, end: datetime | None, now: datetime) -> str:
    if start is None:
        return "unknown"
    if now < start:
        return "upcoming"
    if end is not None and now >= end:
        return "ended"
    return "live"


_NOISE = [
    r"start:\S+ \S+", r"stop:\S+ \S+",
    r"\(\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)?\)",
    r"\|\s*8K EXCLUSIVE.*$",  # "| 8K EXCLUSIVE | US: APPLE TV F1 PPV 5"
    r"^\s*(next|live|end|ended|upcoming)\s*\|",
    r"^US \([^)]*\)\s*\|",  # "US (Paramount 029) |"
    # Package slot numbers are 1-2 digits ("MLB 08 :", "Boxing 8:", "NFL  | 07 -"); "UFC 332:" is an event name.
    r"^(MLB|NFL|NBA|NHL|NCAAF|NCAAB|UEFA|UFC|Boxing)\s+\d{1,2}\s*:\s*",
    r"^(MLB|NFL|NBA|NHL|NCAAF|NCAAB|UEFA|UFC)\s*\|\s*\d{1,2}\s*-\s*",
    r"^(LIVE EVENT|Pay Per View)\s*\d+\s*[-:]?\s*",
    # Slot packages: "US - MLB GAME 08 : RAYS @ YANKEES SEP 25 – 12:05 AM ET / 5:05 AM UK", "US - NFL TEAMS - JETS"
    r"^(MLB|NFL|NBA|NHL)\s+GAME\s+\d+\s*:\s*",
    r"^APPLE F1\s+\d+\s*:\s*",  # e.g. "US - APPLE F1 03 : SINGAPORE GP: RACE ..."
    r"^(MLB|NFL|NBA|NHL)\s+TEAMS\s*[:-]\s*",
    r"\b[a-z]{3}\s+\d{1,2}\s*[–-]\s*\d{1,2}:\d\d\s*(am|pm)\s*ET(\s*/.*)?$",
    r"^(US|UK|CA|BR|PRIME|EPL|F1|NBA TEAM|NHL TEAM|NFL TEAMS)\s*\|\s*",
    r"^(US|UK|BR|EN)\s*-\s*",
    r"@\s*[a-z]{3}\w* \d{1,2} \d{1,2}(:\d\d)?\s*(am|pm)",  # "@ Sep 23 9:30 PM"
    r":\s*(MLB|NBA|MLS|NFL|NHL)\s*\d+\s*$",  # trailing ":NBA 04"
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)\w*\s+\d{1,2}\s+[a-z]{3}\w*\s+\d{1,2}:\d\d(\s+[A-Z]{2,4})?(\s*\([A-Z]{2}\))?",
    r"\|\s*(\d{4}-\d\d-\d\d|\d\d-\d\d-\d{4})\s*\|\s*\d\d:\d\d\s*\((GMT|UTC)\)",
    r"^\d{1,2}(:\d\d)?\s*(am|pm)\s+",
    r"\s+\d{1,2}(:\d\d)?\s*(am|pm)\s*$",
    r"(?:[—–-]\s*|\s)\d{1,2}:\d\d\s*(CET|CEST|ET|EST|EDT|GMT|BST|UK|UTC)\s*$",  # "— 19:00 CET" (now the start time)
    r"\b(FHD|UHD|HEVC|HD|SD)\b|ᴿᴬᵂ|⁶⁰ᶠᵖˢ|◉",
]


def clean_title(name: str) -> str:
    """Strips provider prefixes, timestamps and quality tags: "US| NBC 4 NEW YORK NY (WNBC)" -> "NBC 4 NEW YORK NY (WNBC)"."""
    # A 4K feed is a channel of its own, not a quality tag to drop.
    text = re.sub(r"\bUHD FEED\b", "4K FEED", name, flags=re.I)
    for _ in range(2):  # prefixes can stack ("NEXT | ... ", "US (Paramount 029) | ...")
        for pattern in _NOISE:
            text = re.sub(pattern, " ", text, flags=re.I).strip()
    text = re.sub(r"\s*\|\s*$|^\s*\|\s*", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -:|")
    return text or name.strip()
