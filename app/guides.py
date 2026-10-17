"""Guide data for channels the provider left without one.

- Matching: a channel with no EPG assigned gets an EPG entry Dispatcharr already has under the same channel: the same
  US call sign ("PIX11 (WPIX)" = "CW PIX11 (WPIX) NEW YORK"), or the same name once prefixes, quality tags and symbols
  are dropped, in a compatible country. Event and game slots are skipped (their names change daily).
- Filler: channels that still have no programs get blocks built from what the server knows: an event or game slot's
  title and start time, or the channel's own name by the hour. They're marked `placeholder` so apps can tell."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .backups import QUALITY

CALL_SIGN = re.compile(r"\(([KW][A-Z]{2,3})(?:-[A-Z0-9]+)?\)")
# Guide entries named by call sign alone: "WNET-DT" (tvg_id "WNET-DT.us_locals1"), "WPIX.us".
BARE_CALL_SIGN = re.compile(r"^([KW][A-Z]{2,3})(?:-[A-Z0-9]+)?(?:\.|$)")
ANY_PREFIX = re.compile(r"^([A-Z][A-Z.]{1,5})\s*(\||-|:)\s")
COUNTRY_ALIASES = {"USA": "US", "U.S": "US", "U.S.": "US", "LAT": "US"}


def guide_key(name: str) -> tuple[str | None, str]:
    """(country or None, normalized name). Prefixes that aren't 2-3 letter countries ("PRIME|") count as no country."""
    upper = re.sub(r"[^\x20-\x7e]", " ", (name or "").upper()).strip()
    country = None
    m = ANY_PREFIX.match(upper)
    if m:
        tag = COUNTRY_ALIASES.get(m.group(1), m.group(1))
        country = tag if len(tag) <= 3 and "." not in tag else None
        upper = upper[m.end():]
    upper = QUALITY.sub("", upper)
    upper = re.sub(r"[^A-Z0-9()&+ ]", " ", upper)
    return country, re.sub(r"\s+", " ", upper).strip()


def call_sign(name: str) -> str | None:
    m = CALL_SIGN.search((name or "").upper())
    return m.group(1) if m else None


def entry_call_sign(entry: dict) -> str | None:
    """A guide entry's call sign: "(WPIX)" in its name, or a name or tvg_id that is just the call sign."""
    for text in (entry.get("name"), entry.get("tvg_id")):
        upper = (text or "").upper().strip()
        m = CALL_SIGN.search(upper) or BARE_CALL_SIGN.match(upper)
        if m:
            return m.group(1)
    return None


def match(name: str, entries: list[dict], source_rank: dict[int, int]) -> dict | None:
    """The best EPG entry for a channel name, or None. `source_rank`: EPG source id -> preference (lower first)."""
    sign = call_sign(name)
    country, key = guide_key(name)
    found = []
    for e in entries:
        if not e.get("tvg_id"):
            continue
        if sign:
            if entry_call_sign(e) == sign:
                found.append(e)
            continue
        c, k = guide_key(e.get("name") or "")
        if k == key and (c == country or c is None or country is None):
            found.append(e)
    found.sort(key=lambda e: (source_rank.get(e.get("epg_source"), 99), e["id"]))
    return found[0] if found else None


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _block(title: str, start: datetime, end: datetime, **extra) -> dict:
    return {"title": title, "subtitle": None, "description": None, "start": _iso(start), "end": _iso(end),
            "live": None, "new": None, "placeholder": True} | extra


def filler(title: str, event: dict | None, start: datetime, end: datetime) -> list[dict]:
    """Blocks for [start, end): hourly ones with the channel's name or, on an event slot, the event in its time with
    "Coming up: ..." before it and "Off air" after."""
    def hourly(a: datetime, b: datetime, label: str = title) -> list[dict]:
        out, t = [], a
        while t < b:
            nxt = min(b, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            out.append(_block(label, t, nxt))
            t = nxt
        return out

    ev_start = datetime.fromisoformat(event["start"].replace("Z", "+00:00")) if event and event.get("start") else None
    if event and ev_start is None:
        return [_block("No event scheduled" if (event["title"] or "").upper() == "NO EVENT" else event["title"] or title, start, end)]
    if ev_start is None:
        return hourly(start, end)
    ev_end = datetime.fromisoformat(event["end"].replace("Z", "+00:00")) if event.get("end") else ev_start + timedelta(hours=3)
    name = event["title"] or title
    if ev_end <= start:
        return hourly(start, end, "Off air")
    if ev_start >= end:
        return hourly(start, end, f"Coming up: {name}")
    blocks = hourly(start, ev_start, f"Coming up: {name}") if ev_start > start else []
    # The event keeps its own times (the app clips it to the screen), so "started 7:00" reads right.
    blocks.append(_block(name, ev_start, ev_end, live=event.get("status") == "live",
                         placeholder=False))
    blocks += hourly(ev_end, end, "Off air") if ev_end < end else []
    return blocks
