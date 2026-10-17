"""Upcoming episodes (Sonarr) and movie release dates (Radarr), merged into one dated list. Read-only; the API keys
never leave the server."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .config import Settings

log = logging.getLogger("clipstream.calendar")
CACHE_SECONDS = 600
RELEASES = [("inCinemas", "In theaters"), ("digitalRelease", "Digital"), ("physicalRelease", "Blu-ray / DVD")]


def _describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}" + (" (check the API key)" if e.response.status_code == 401 else "")
    if isinstance(e, httpx.RequestError):
        return f"can't reach {e.request.url.host}: {type(e).__name__}"
    return type(e).__name__


def _poster(images: list[dict] | None) -> str | None:
    for kind in ("poster", "fanart", "banner"):
        for img in images or []:
            if img.get("coverType") == kind and img.get("remoteUrl"):
                return img["remoteUrl"]
    return None


def _backdrop(images: list[dict] | None) -> str | None:
    return next((i["remoteUrl"] for i in images or [] if i.get("coverType") == "fanart" and i.get("remoteUrl")), None)


class Calendar:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        # Last outcome per source, shown on /api/health so misconfiguration is visible without container logs.
        self.status: dict[str, str] = {}

    async def _get(self, base: str, key: str, path: str, params: dict) -> list[dict]:
        async with httpx.AsyncClient(base_url=base, timeout=30, headers={"X-Api-Key": key}) as client:
            r = await client.get(path, params=params)
            r.raise_for_status()
            return r.json()

    async def episodes(self, start: date, end: date) -> list[dict]:
        s = self.settings
        if not (s.sonarr_url and s.sonarr_api_key):
            return []
        raw = await self._get(s.sonarr_url, s.sonarr_api_key, "/api/v3/calendar",
                              {"start": start.isoformat(), "end": end.isoformat(), "includeSeries": "true"})
        out = []
        for e in raw:
            series = e.get("series") or {}
            if not e.get("airDateUtc"):
                continue
            out.append({
                "type": "episode",
                "date": e["airDateUtc"],
                "title": series.get("title"),
                "subtitle": f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}" + (f" · {e['title']}" if e.get("title") else ""),
                "release": series.get("network"),
                "overview": e.get("overview") or series.get("overview"),
                "poster": _poster(series.get("images")),
                "backdrop": _backdrop(series.get("images")),
                "inLibrary": bool(e.get("hasFile")),
                "premiere": e.get("episodeNumber") == 1,
                "tvdbId": series.get("tvdbId"),
            })
        return out

    async def movies(self, start: date, end: date) -> list[dict]:
        s = self.settings
        if not (s.radarr_url and s.radarr_api_key):
            return []
        raw = await self._get(s.radarr_url, s.radarr_api_key, "/api/v3/calendar",
                              {"start": start.isoformat(), "end": end.isoformat()})
        out = []
        lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
        for m in raw:
            for field, label in RELEASES:
                when = m.get(field)
                if not when or not (lo <= when[:10] < hi):
                    continue
                out.append({
                    "type": "movie",
                    "date": when if "T" in when else f"{when}T00:00:00Z",
                    "title": m.get("title"),
                    "subtitle": str(m.get("year") or ""),
                    "release": label,
                    "overview": m.get("overview"),
                    "poster": _poster(m.get("images")),
                    "backdrop": _backdrop(m.get("images")),
                    "inLibrary": bool(m.get("hasFile")),
                    "premiere": False,
                    "tmdbId": m.get("tmdbId"),
                })
        return out

    async def upcoming(self, days_back: int, days_ahead: int) -> list[dict]:
        today = datetime.now(timezone.utc).date()
        start, end = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
        key = (start, end)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        results = await asyncio.gather(self.episodes(start, end), self.movies(start, end), return_exceptions=True)
        items = []
        for name, configured, r in (("sonarr", self.settings.sonarr_url and self.settings.sonarr_api_key, results[0]),
                                    ("radarr", self.settings.radarr_url and self.settings.radarr_api_key, results[1])):
            if not configured:
                self.status[name] = "not configured"
            elif isinstance(r, Exception):
                log.warning("%s failed: %r", name, r)
                self.status[name] = f"error: {_describe(r)}"
            else:
                self.status[name] = f"ok ({len(r)} items)"
                items.extend(r)
        items.sort(key=lambda i: (i["date"], i["title"] or ""))
        self._cache[key] = (time.monotonic(), items)
        return items
