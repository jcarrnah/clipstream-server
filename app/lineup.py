"""Channel upkeep after each refresh (on with DISPATCHARR_MANAGE_CHANNELS=true; it changes channels, so the Dispatcharr
user needs the Admin level):

- Duplicates: providers sometimes list a channel twice under different stream ids, and auto-sync then makes two
  channels with the same name. The first one (lowest id) stays; the others are hidden and their streams become backups
  on it. A channel hidden here is shown again once it no longer has a same-named twin (game slots get renamed).
- Numbers: Dispatcharr's auto-sync renumbers its channels on every provider refresh. A channel number set as an
  override ("pinned") is left alone, so a layout pinned once stays. A channel that shows up later gets pinned after the
  highest pinned number in its group. Groups without pinned numbers keep Dispatcharr's own numbering.
- Guides: a channel with no EPG gets a matching one Dispatcharr already has (app/guides.py).
- Backup streams (app/backups.py)."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from . import events, guides
from .backups import Backups
from .dispatcharr import Dispatcharr

log = logging.getLogger("clipstream.lineup")


def pinned(channel: dict) -> float | None:
    return (channel.get("override") or {}).get("channel_number")


def duplicates(channels: list[dict]) -> dict[int, list[int]]:
    """Keeper channel id -> ids of its same-named twins in the same group (auto-synced channels only)."""
    same: dict[tuple, list[dict]] = defaultdict(list)
    for c in channels:
        if c.get("auto_created") and c.get("streams"):
            same[(c.get("channel_group_id"), " ".join(c["name"].upper().split()))].append(c)
    out = {}
    for members in same.values():
        if len(members) > 1:
            ids = sorted(c["id"] for c in members)
            out[ids[0]] = ids[1:]
    return out


def new_numbers(channels: list[dict], hidden: set[int]) -> dict[int, float]:
    """Pins for visible auto-synced channels without one: after the highest pinned number in their group, skipping
    numbers other channels use."""
    visible = [c for c in channels if not c.get("hidden_from_output") and c["id"] not in hidden]
    todo = [c for c in visible if c.get("auto_created") and pinned(c) is None]
    todo_ids = {c["id"] for c in todo}
    taken = {n for c in visible if c["id"] not in todo_ids and (n := c.get("effective_channel_number")) is not None}
    top: dict[int, float] = {}
    for c in visible:
        if (n := pinned(c)) is not None:
            top[c.get("channel_group_id")] = max(n, top.get(c.get("channel_group_id"), n))
    out = {}
    for c in sorted(todo, key=lambda c: c["id"]):
        group = c.get("channel_group_id")
        if group not in top:
            continue
        n = int(top[group]) + 1
        while n in taken:
            n += 1
        out[c["id"]] = top[group] = n
        taken.add(n)
    return out


def source_stream(channel: dict) -> int | None:
    return (channel.get("source_stream") or {}).get("id")


class Lineup:
    def __init__(self, dispatcharr: Dispatcharr):
        self.dispatcharr = dispatcharr
        self.backups = Backups(dispatcharr)
        self._hidden: set[int] = set()  # twins hidden by this server (shown again when the twin goes away)
        self._guide_checked: set[int] = set()  # channels already looked up (matched or not)
        self._guide_sources: tuple = ()  # EPG sources and their entry counts when those lookups ran
        self.status: dict = {}

    async def run(self, channels: list[dict]):
        by_id = {c["id"]: c for c in channels}
        twins = duplicates(channels)
        hide = {t for ts in twins.values() for t in ts}
        hidden = shown = 0
        for cid in sorted(hide):
            c = by_id[cid]
            if not c.get("hidden_from_output"):
                # Its pin would keep reserving a number nobody sees.
                body = {"hidden_from_output": True} | ({"override": {"channel_number": None}} if pinned(c) is not None else {})
                await self.dispatcharr.update_channel(cid, body)
                c["hidden_from_output"] = True
                hidden += 1
            self._hidden.add(cid)
        for cid in sorted(self._hidden - hide):
            c = by_id.get(cid)
            if c is not None and c.get("hidden_from_output"):
                await self.dispatcharr.update_channel(cid, {"hidden_from_output": False})
                c["hidden_from_output"] = False
                shown += 1
        self._hidden &= hide

        numbers = new_numbers(channels, hide)
        if numbers:
            await self.dispatcharr.edit_channels([{"id": cid, "override": {"channel_number": n}} for cid, n in numbers.items()])
            for cid, n in numbers.items():
                by_id[cid]["override"] = (by_id[cid].get("override") or {}) | {"channel_number": n}
                by_id[cid]["effective_channel_number"] = n

        guided = await self._match_guides(channels)

        extras = {keeper: [s for t in ts if (s := source_stream(by_id[t])) is not None] for keeper, ts in twins.items()}
        await self.backups.run([c for c in channels if not c.get("hidden_from_output")], extras)
        self.status = {"twinsHidden": len(hide), "pinned": len(numbers), "guidesMatched": guided, "backups": self.backups.status,
                       "at": datetime.now().astimezone().isoformat()}
        if hidden or shown or numbers:
            log.info("lineup: hid %d twins, showed %d, pinned %d new channels", hidden, shown, len(numbers))

    async def _match_guides(self, channels: list[dict]) -> int:
        """Assigns a matching EPG entry (as an override) to auto-synced channels without one. Each channel is looked up
        once, and again when the EPG sources change; event slots are skipped."""
        sources = await self.dispatcharr.epg_sources()
        # A new guide source (or one that grew) may have what earlier lookups didn't find.
        fingerprint = tuple(sorted((s["id"], s.get("epg_data_count")) for s in sources))
        if fingerprint != self._guide_sources:
            self._guide_sources, self._guide_checked = fingerprint, set()
        todo = [c for c in channels if c.get("auto_created") and not c.get("hidden_from_output")
                and not c.get("effective_epg_data_id") and c["id"] not in self._guide_checked
                and not events.looks_like_event(c.get("effective_name") or c["name"])]
        if not todo:
            return 0
        entries = await self.dispatcharr.epg_entries()
        # EPG sources named like an M3U account rank by that account's priority.
        accounts = {a["name"]: a.get("priority") or 0 for a in await self.dispatcharr.m3u_accounts()}
        rank = {s["id"]: -accounts.get(s["name"], -1000) for s in sources}
        matched = 0
        for c in todo:
            self._guide_checked.add(c["id"])
            entry = guides.match(c.get("effective_name") or c["name"], entries, rank)
            if entry is None:
                continue
            await self.dispatcharr.update_channel(c["id"], {"override": {"epg_data_id": entry["id"], "tvg_id": entry["tvg_id"]}})
            c["effective_epg_data_id"], c["effective_tvg_id"] = entry["id"], entry["tvg_id"]
            matched += 1
        if matched:
            log.info("guides: matched %d channels to EPG entries", matched)
        return matched
