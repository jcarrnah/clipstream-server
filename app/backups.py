"""Backup streams: gives every auto-synced channel the same channel from the other IPTV accounts, so Dispatcharr can move
to the next stream when an account is at its connection limit (or a stream fails).

A stream "is the same channel" when its name matches once the country prefix, quality tags and symbols are dropped
("US - ESPN HD ◉" = "US| ESPN ᴴᴰ"), within the same country. A channel's streams are ordered by account priority
(Dispatcharr's M3U account "priority", highest first), the creating stream first among equals. When an account has the
name twice, the stream with the creator's provider id wins (two accounts on one provider share ids).

Only channels whose streams changed, or all of them after an account refreshed, are checked again, so a renamed game
slot loses a backup whose name no longer matches (it may carry another game) and gets the right one back."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from .dispatcharr import Dispatcharr

log = logging.getLogger("clipstream.backups")

QUALITY = re.compile(r"\b(UHD|FHD|HD|SD|4K|HEVC|RAW|EAST)\b")
# Only "US - " / "US| " style prefixes; "LAT" is a region some providers file US Spanish channels under.
PREFIX = re.compile(r"^([A-Z]{2,3})\s*(\||-)\s")
ALIASES = {"LAT": "US"}


def country(name: str) -> str | None:
    m = PREFIX.match(name.upper())
    return m.group(1) if m else None


def norm(name: str) -> str:
    name = re.sub(r"[^\x20-\x7e]", " ", name.upper()).strip()
    name = PREFIX.sub("", name)
    name = QUALITY.sub("", name)
    name = re.sub(r"[^A-Z0-9()&+ ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def key(name: str, alias: bool = False) -> tuple[str | None, str]:
    c = country(name)
    return (ALIASES.get(c, c) if alias else c), norm(name)


def search_term(name: str) -> str | None:
    """The longest word of the name, for Dispatcharr's "name contains" filter; short names ("FX", "A&E") as a whole,
    and the first word as written when even that is too short ("E!")."""
    words = sorted(re.findall(r"[A-Z0-9]+", norm(name)), key=len, reverse=True)
    if words and len(words[0]) >= 3:
        return words[0]
    if len(norm(name)) >= 2:
        return norm(name)
    first = PREFIX.sub("", name.upper().strip()).split()
    return first[0] if first else None


def ordered(own: dict, candidates: list[dict], priority: dict[int, int], extra: list[int] = ()) -> list[int]:
    """Stream ids for a channel: its own stream, then `extra` (its hidden twins' streams, same account), plus, per
    other account, the best same-channel candidate, highest account priority first."""
    # "LAT - TELEMUNDO 47" matches another LAT stream or a US one; a US channel never matches LAT (Latin American) feeds.
    want = {key(own["name"]), key(own["name"], alias=True)}

    def preference(s):  # same provider stream, then a plain name over "ᴴᴰ ⁶⁰ᶠᵖˢ"-style variants; stable across runs
        same_id = s.get("stream_id") is not None and s.get("stream_id") == own.get("stream_id")
        return not same_id, not s["name"].isascii(), len(s["name"]), s["id"]

    best: dict[int, dict] = {}
    for s in candidates:
        acct = s.get("m3u_account")
        if acct == own.get("m3u_account") or acct not in priority or key(s["name"]) not in want:
            continue
        if acct not in best or preference(s) < preference(best[acct]):
            best[acct] = s
    streams = [own] + [{"id": s, "m3u_account": own.get("m3u_account")} for s in extra if s != own["id"]] + list(best.values())
    rank = {id(s): i for i, s in enumerate(streams)}
    streams.sort(key=lambda s: (-priority.get(s.get("m3u_account"), 0), s is not own, rank[id(s)]))
    return [s["id"] for s in streams]


class Backups:
    def __init__(self, dispatcharr: Dispatcharr):
        self.dispatcharr = dispatcharr
        self._checked: dict[int, tuple] = {}  # channel id -> (its stream ids, accounts' refresh times) when checked
        self.status: dict = {}

    async def run(self, channels: list[dict], extras: dict[int, list[int]] | None = None):
        extras = extras or {}
        accounts = [a for a in await self.dispatcharr.m3u_accounts() if a.get("is_active")]
        if len(accounts) < 2:
            return
        priority = {a["id"]: a.get("priority") or 0 for a in accounts}
        refreshed = tuple(sorted((a["id"], a.get("updated_at") or "") for a in accounts))
        todo = [c for c in channels if c.get("auto_created") and c.get("streams")
                and self._checked.get(c["id"]) != (tuple(c["streams"]), refreshed, tuple(extras.get(c["id"], ())))]
        if not todo:
            return
        info = await self.dispatcharr.streams_by_id({s for c in todo for s in c["streams"]})
        changed = 0
        for c in todo:
            own = next((info[s] for s in c["streams"] if s in info and info[s].get("m3u_account") == c.get("auto_created_by")), None)
            if own is None or own.get("m3u_account") not in priority:
                self._checked[c["id"]] = (tuple(c["streams"]), refreshed, tuple(extras.get(c["id"], ())))
                continue
            term = search_term(own["name"])
            others = [a for a in priority if a != own["m3u_account"]]
            candidates = await self.dispatcharr.streams(name=term, m3u_account=",".join(map(str, others))) if term else []
            order = ordered(own, candidates, priority, extras.get(c["id"], ()))
            # Keep streams added by hand (custom streams) after the provider ones.
            order += [s for s in c["streams"] if s in info and info[s].get("is_custom") and s not in order]
            if order != c["streams"]:
                await self.dispatcharr.set_streams(c["id"], order)
                c["streams"] = order
                changed += 1
            self._checked[c["id"]] = (tuple(order), refreshed, tuple(extras.get(c["id"], ())))
        alive = {c["id"] for c in channels}
        self._checked = {k: v for k, v in self._checked.items() if k in alive}
        self.status = {"checked": len(todo), "changed": changed, "at": datetime.now().astimezone().isoformat()}
        log.info("backup streams: checked %d channels, updated %d", len(todo), changed)
