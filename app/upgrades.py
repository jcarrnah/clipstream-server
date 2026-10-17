"""Upgrades: movies and episodes whose recent imports in Radarr/Sonarr only replaced a file that was already there.

Jellyfin's "recently added" goes by when the file showed up, so a better copy of an old movie looks new. Radarr and
Sonarr record an upgrade as an import plus a "file deleted, reason Upgrade" for the same title at the same time. A
title counts as upgraded when every import in the window was one of those; a title that arrived for the first time
in the window (even if upgraded since) is new. The app leaves upgraded titles out of its Recently Added rows."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings

log = logging.getLogger("clipstream.upgrades")
WINDOW_DAYS = 45
CACHE_SECONDS = 600
REFRESH_SECONDS = 300  # kept warm in the background: a cold fetch takes ~8 s (Sonarr history)
PAIR_SECONDS = 300  # an upgrade's import and its "file deleted" land within seconds of each other


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def only_upgrades(records: list[dict], item_key, deleted_event: str) -> set:
    """Keys of the titles whose every import in `records` came with an upgrade deletion of the same title."""
    imports, upgrades = defaultdict(list), defaultdict(list)
    for r in records:
        key = item_key(r)
        if key is None:
            continue
        if r.get("eventType") == "downloadFolderImported":
            imports[key].append(_t(r["date"]))
        elif r.get("eventType") == deleted_event and (r.get("data") or {}).get("reason") == "Upgrade":
            upgrades[key].append(_t(r["date"]))
    out = set()
    for key, times in imports.items():
        deleted = upgrades.get(key, [])
        if all(any(abs((t - d).total_seconds()) <= PAIR_SECONDS for d in deleted) for t in times):
            out.add(key)
    return out


class Upgrades:
    """Callers get the last result at once; it's refreshed in the background, and a first fetch is shared by everyone
    waiting for it (the app's Home and Recently Updated asked together and each ran Sonarr's history)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: tuple[float, dict] | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._keep_warm())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _keep_warm(self) -> None:
        while True:
            try:
                await self._refresh()
            except Exception as e:
                log.warning("upgrades refresh: %s", e)
            await asyncio.sleep(REFRESH_SECONDS)

    async def _history(self, base: str, key: str, params: dict) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with httpx.AsyncClient(base_url=base, timeout=60, headers={"X-Api-Key": key}) as client:
            r = await client.get("/api/v3/history/since", params={"date": since, **params})
            r.raise_for_status()
            return r.json()

    async def get(self) -> dict:
        seen = self._cache
        if seen is None or time.monotonic() - seen[0] >= CACHE_SECONDS:
            await self._refresh(seen)
        return self._cache[1]

    async def _refresh(self, seen=False) -> None:
        """Fetch again; with `seen` (the cache a caller found stale or missing), not if another caller already has."""
        async with self._lock:
            if seen is not False and self._cache is not seen:
                return
            self._cache = (time.monotonic(), await self._fetch())

    async def _fetch(self) -> dict:
        s = self.settings
        result: dict = {"days": WINDOW_DAYS, "movies": {"tmdb": []}, "episodes": {"tvdb": [], "keys": []}}

        async def movies():
            if not (s.radarr_url and s.radarr_api_key):
                return
            try:
                records = await self._history(s.radarr_url, s.radarr_api_key, {"includeMovie": "true"})
                found = only_upgrades(records, lambda r: (r.get("movie") or {}).get("tmdbId") or None, "movieFileDeleted")
                result["movies"]["tmdb"] = sorted(found)
            except Exception as e:  # leave movies unfiltered rather than fail
                log.warning("radarr history: %s", e)

        async def episodes():
            if not (s.sonarr_url and s.sonarr_api_key):
                return
            try:
                records = await self._history(s.sonarr_url, s.sonarr_api_key, {"includeSeries": "true", "includeEpisode": "true"})

                def episode(r):
                    ep, series = r.get("episode") or {}, r.get("series") or {}
                    if not series.get("tvdbId") or ep.get("seasonNumber") is None:
                        return None
                    return series["tvdbId"], ep["seasonNumber"], ep.get("episodeNumber"), ep.get("tvdbId")

                eps = only_upgrades(records, episode, "episodeFileDeleted")
                result["episodes"]["tvdb"] = sorted({e[3] for e in eps if e[3]})
                # Series tvdb id, season, episode: for Jellyfin episodes without their own tvdb id.
                result["episodes"]["keys"] = sorted(f"{e[0]}:{e[1]}:{e[2]}" for e in eps)
            except Exception as e:
                log.warning("sonarr history: %s", e)

        await asyncio.gather(movies(), episodes())
        return result
