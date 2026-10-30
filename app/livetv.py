"""Live TV catalog: channels, categories and the guide, cached from Dispatcharr and refreshed in the background."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from . import events, guides, names, streams
from .lineup import Lineup
from .categories import Category
from .config import Settings
from .dispatcharr import Dispatcharr

log = logging.getLogger("clipstream.livetv")
LEAGUE_SLOT = re.compile(r"^\s*(MLB|NFL|NBA|NHL|NCAAF|NCAAB)\b", re.I)
UTC = timezone.utc


class LiveTv:
    def __init__(self, settings: Settings, dispatcharr: Dispatcharr, categories: list[Category]):
        self.settings = settings
        self.dispatcharr = dispatcharr
        self.categories = categories
        self.channels: list[dict] = []
        self.groups: dict[int, str] = {}
        self.logos: dict[int, dict] = {}
        self.programs: dict[str, list[dict]] = {}  # tvg_id -> programs sorted by start
        self.channels_at: datetime | None = None
        self.guide_at: datetime | None = None
        self._logo_cache: dict[int, tuple[bytes, str]] = {}
        self._tasks: list[asyncio.Task] = []
        self.lineup = Lineup(dispatcharr) if settings.dispatcharr_manage_channels else None
        self._lineup_task: asyncio.Task | None = None

    # region refresh

    async def start(self):
        await asyncio.gather(self.refresh_channels(), self.refresh_guide(), return_exceptions=True)
        self._tasks = [
            asyncio.create_task(self._every(self.settings.channel_refresh_seconds, self.refresh_channels)),
            asyncio.create_task(self._every(self.settings.guide_refresh_seconds, self.refresh_guide)),
        ]

    async def stop(self):
        for t in self._tasks + ([self._lineup_task] if self._lineup_task else []):
            t.cancel()

    @staticmethod
    async def _every(seconds: int, job):
        while True:
            await asyncio.sleep(seconds)
            try:
                await job()
            except Exception:  # keep serving the last good data
                log.exception("refresh failed")

    async def refresh_channels(self):
        channels, groups, logos = await asyncio.gather(self.dispatcharr.channels(), self.dispatcharr.groups(), self.dispatcharr.logos())
        self.channels, self.groups, self.logos = channels, groups, logos
        self.channels_at = datetime.now(UTC)
        log.info("loaded %d channels", len(channels))
        if self.lineup and not (self._lineup_task and not self._lineup_task.done()):
            self._lineup_task = asyncio.create_task(self._run_lineup(channels))

    async def _run_lineup(self, channels: list[dict]):
        try:
            await self.lineup.run(channels)
        except Exception as e:
            self.lineup.status = {"error": str(e)[:200]}
            log.exception("channel upkeep failed")

    async def refresh_guide(self):
        now = datetime.now(UTC)
        grid = await self.dispatcharr.guide(now - timedelta(hours=2), now + timedelta(hours=self.settings.guide_hours_ahead))
        programs: dict[str, list[dict]] = defaultdict(list)
        for p in grid:
            if p.get("tvg_id"):
                programs[p["tvg_id"]].append(p)
        for plist in programs.values():
            plist.sort(key=lambda p: p["start_time"])
        self.programs = dict(programs)
        self.guide_at = now
        log.info("loaded guide: %d programs for %d channels", len(grid), len(programs))

    # endregion

    # region views

    def _programs_for(self, channel: dict) -> list[dict]:
        return self.programs.get(channel.get("effective_tvg_id") or channel.get("tvg_id") or "", [])

    @staticmethod
    def _program(p: dict | None) -> dict | None:
        if not p:
            return None
        fix = names.repair
        return {"title": fix(p.get("title")), "subtitle": fix(p.get("sub_title")), "description": fix(p.get("description")),
                "start": p.get("start_time"), "end": p.get("end_time"), "live": p.get("is_live"), "new": p.get("is_new")}

    def _now_next(self, channel: dict, now: datetime) -> tuple[dict | None, dict | None]:
        iso = now.isoformat().replace("+00:00", "Z")
        plist = self._programs_for(channel)
        current = next((p for p in plist if p["start_time"] <= iso < p["end_time"]), None)
        upcoming = next((p for p in plist if p["start_time"] > iso), None)
        return self._program(current), self._program(upcoming)

    def stream_url(self, uuid: str) -> str:
        """A signed link to this server's relay when STREAM_PUBLIC_URL is set, else Dispatcharr's own link."""
        if self.settings.stream_public_url:
            secret = streams.secret_from(self.settings.dispatcharr_password, self.settings.stream_secret)
            return streams.signed_url(self.settings.stream_public_url, secret, uuid)
        return self.dispatcharr.stream_url(self.settings.dispatcharr_public_url, uuid)

    def channel_view(self, channel: dict, now: datetime) -> dict | None:
        """The app's view of one channel, or None for an event that has already ended."""
        name = channel.get("effective_name") or channel.get("name") or ""
        group = self.groups.get(channel.get("effective_channel_group_id") or channel.get("channel_group_id"))
        event = events.parse(name, now, self.settings.local_timezone) if events.looks_like_event(name) else None
        current, upcoming = self._now_next(channel, now)
        if (event and event.time_only and event.status == "live" and not LEAGUE_SLOT.match(name)
                and not (current and current.get("live"))):
            # "8:00 pm" with no date could be a stale slot from weeks ago; only trust it when the guide agrees.
            # League package slots (NFL/NHL/...) are rewritten daily, so their clock times are today's.
            event.status = "unknown"
        if event and event.status == "ended":
            return None
        cats = [c for c in self.categories if c.matches(group, name)]
        title = names.tidy(event.title if event else events.clean_title(name))
        if event:
            event.title = title
        logo_id = channel.get("effective_logo_id") or channel.get("logo_id")
        return {
            "id": channel["id"],
            "number": channel.get("effective_channel_number") or channel.get("channel_number"),
            "name": title,
            "providerName": name,
            "group": group,
            "categories": [c.id for c in cats],
            "featured": any(c.featured(title) for c in cats),
            "logo": f"/api/livetv/logo/{logo_id}" if logo_id else None,
            "stream": self.stream_url(channel["uuid"]),
            "event": event.as_dict() if event else None,
            "now": current,
            "next": upcoming,
        }

    def all_views(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        views = sorted((v for c in self.channels if not c.get("hidden_from_output") and (v := self.channel_view(c, now))), key=_order)
        # The same event often comes from two provider groups (or as a backup feed): keep the first.
        seen, unique = set(), []
        for v in views:
            ev = v["event"]
            key = (ev["title"].lower(), ev["start"]) if ev else None
            if key and key in seen:
                continue
            seen.add(key)
            unique.append(v)
        return unique

    def category_list(self, now: datetime | None = None) -> list[dict]:
        views = self.all_views(now)
        out = []
        for c in self.categories:
            members = [v for v in views if c.id in v["categories"]]
            live = sum(1 for v in members if (v["event"] or {}).get("status") == "live")
            out.append({**c.summary(), "channelCount": len(members), "liveEvents": live})
        return out

    def channels_in(self, category: str | None, now: datetime | None = None) -> list[dict]:
        views = self.all_views(now)
        return [v for v in views if category is None or category in v["categories"]]

    def guide(self, channel_ids: set[int] | None, start: datetime, end: datetime) -> dict[str, list[dict]]:
        """Programs per channel id. Channels without any get filler (app/guides.py): an event slot's own event, or
        hourly blocks with the channel's name, marked `placeholder`."""
        s, e = start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")
        now = datetime.now(UTC)
        out = {}
        for c in self.channels:
            if c.get("hidden_from_output") or (channel_ids is not None and c["id"] not in channel_ids):
                continue
            plist = [self._program(p) | {"placeholder": False} for p in self._programs_for(c) if p["end_time"] > s and p["start_time"] < e]
            if not plist and (view := self.channel_view(c, now)):
                plist = guides.filler(view["name"], view["event"], start, end)
            if plist:
                out[str(c["id"])] = plist
        return out

    # endregion

    async def logo(self, logo_id: int) -> tuple[bytes, str] | None:
        if logo_id in self._logo_cache:
            return self._logo_cache[logo_id]
        info = self.logos.get(logo_id)
        if not info:
            return None
        urls = [u for u in (info.get("cache_url"), info.get("url")) if u]
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                        self._logo_cache[logo_id] = (r.content, r.headers["content-type"])
                        return self._logo_cache[logo_id]
                except httpx.HTTPError:
                    continue
        return None


def _order(v: dict) -> tuple:
    """Live favorite-team games first, then live games, regular channels, upcoming events by start, undated ones."""
    ev = v["event"]
    number = v["number"] or 99999
    if not ev:
        return (2, 0 if v["featured"] else 1, number, "")
    status, start = ev["status"], ev["start"] or ""
    if status == "live":
        return (0 if v["featured"] else 1, 0, number, start)
    if status == "upcoming":
        return (3, 0 if v["featured"] else 1, start, number)
    return (5, 0 if v["featured"] else 1, number, "")
