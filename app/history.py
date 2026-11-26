"""Watch history kept by this server, for the year in review (app/wrapped.py). Both sources are read with
JELLYFIN_API_KEY and kept in HISTORY_DB (SQLite):

- Jellyfin's activity log: a "started" and a "finished" entry for every play since the server was set up. It's copied
  here every few minutes (Jellyfin's retention setting can trim it), with each item's details at the time: a file that
  gets replaced gets a new id, and the old one stops answering. Items already gone are matched by the title in the
  entry's text.
- The active sessions, every 30 s: the time actually spent playing (pauses don't count), per user, device and item.
  The log alone only has the time between start and stop. Sampler runs are recorded, so a play the sampler watched
  from start to stop is never estimated from the log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("clipstream.history")

SYNC_SECONDS = 600
SAMPLE_SECONDS = 30
# Playing again within this long continues the same segment; after a longer pause (or a restart) it's a new one.
SEGMENT_GAP = 600
PAGE = 1000
ITEM_FIELDS = "Genres,ProductionYear,RunTimeTicks,SeriesId,SeriesName,ParentIndexNumber,IndexNumber"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, type TEXT, user_id TEXT, item_id TEXT, at REAL, title TEXT, device TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY, kind TEXT, name TEXT, series_id TEXT, series_name TEXT, season INTEGER, episode INTEGER,
    runtime REAL, genres TEXT, year INTEGER, image_id TEXT, resolved TEXT, at REAL);
CREATE TABLE IF NOT EXISTS series (
    series_id TEXT PRIMARY KEY, name TEXT, genres TEXT, year INTEGER, runtime REAL, at REAL);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, device_id TEXT, device TEXT, client TEXT, item_id TEXT,
    started_at REAL, last_at REAL, seconds REAL, position REAL);
CREATE INDEX IF NOT EXISTS segments_at ON segments (started_at);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, last_at REAL);
CREATE TABLE IF NOT EXISTS seen (user_id TEXT, year INTEGER, state TEXT, at REAL, PRIMARY KEY (user_id, year));
"""

EVENT_TEXT = re.compile(r"^.*? (?:is playing|has finished playing) (.*) on (.*)$")


def parse_time(value: str | None) -> float | None:
    """Jellyfin's timestamps ("2026-11-26T03:41:25.1233777Z", seven fraction digits) as epoch seconds."""
    if not value:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def parse_text(text: str) -> tuple[str | None, str | None]:
    """The title and device from "sam is playing Show - Episode on Living Room"."""
    m = EVENT_TEXT.match(text or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("&", "and"))


def resolve_title(title: str, movies: dict[str, dict], series: dict[str, dict]) -> tuple[str, dict, str | None] | None:
    """A gone item's title text matched to what the library has now: ("Movie", movie, None) or ("Episode", series,
    episode name). Episodes read "Series - Episode"; either part can contain " - ", so the longest series name wins."""
    if norm(title) in movies:
        return "Movie", movies[norm(title)], None
    parts = title.split(" - ")
    for i in range(len(parts) - 1, 0, -1):
        hit = series.get(norm(" - ".join(parts[:i])))
        if hit:
            return "Episode", hit, " - ".join(parts[i:])
    return None


def _genres(item: dict) -> str:
    return json.dumps(item.get("Genres") or [])


def _runtime(item: dict) -> float | None:
    ticks = item.get("RunTimeTicks")
    return ticks / 1e7 if ticks else None


class History:
    def __init__(self, path: str, jellyfin_url: str, api_key: str):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.client = httpx.AsyncClient(base_url=jellyfin_url, timeout=60,
                                        headers={"Authorization": f'MediaBrowser Token="{api_key}"'})
        self.open: dict[tuple[str, str, str], tuple[int, float, float]] = {}
        self.wanted: set[str] = set()
        self.run_id: int | None = None
        self.last_poll: float | None = None
        self.tasks: list[asyncio.Task] = []
        self.status: dict = {"syncedAt": None, "sampledAt": None, "error": None, "playing": 0}
        now = time.time()
        for row in self.db.execute("SELECT id, user_id, device_id, item_id, last_at, seconds FROM segments WHERE last_at > ?",
                                   (now - SEGMENT_GAP,)):
            self.open[(row[1], row[2], row[3])] = (row[0], row[4], row[5])

    # --- running -------------------------------------------------------------------------------------------------

    def start(self):
        self.tasks = [asyncio.create_task(self._loop(self.sync, SYNC_SECONDS)),
                      asyncio.create_task(self._loop(self.sample, SAMPLE_SECONDS))]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        self.db.close()

    async def _loop(self, step, every: int):
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep going: Jellyfin restarts, network blips
                self.status["error"] = f"{step.__name__}: {e}"
                log.warning("history %s: %s", step.__name__, e)
            await asyncio.sleep(every)

    def counts(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {"events": q("SELECT count(*) FROM events"), "items": q("SELECT count(*) FROM items"),
                "segments": q("SELECT count(*) FROM segments"),
                "since": q("SELECT min(at) FROM events"), **self.status}

    # --- the sessions sampler ------------------------------------------------------------------------------------

    async def sample(self):
        r = await self.client.get("/Sessions", params={"activeWithinSeconds": 300})
        r.raise_for_status()
        self.record(r.json(), time.time())

    def record(self, sessions: list[dict], now: float):
        """Adds the time since the last poll to every session that's playing (not paused)."""
        if self.last_poll is None or now - self.last_poll > 3 * SAMPLE_SECONDS:
            # First poll, or a gap (Jellyfin didn't answer): what happened in between wasn't watched.
            elapsed = 0.0
            self.run_id = self.db.execute("INSERT INTO runs (started_at, last_at) VALUES (?, ?)", (now, now)).lastrowid
        else:
            elapsed = now - self.last_poll
            self.db.execute("UPDATE runs SET last_at = ? WHERE id = ?", (now, self.run_id))
        self.last_poll = now
        playing = 0
        for s in sessions:
            item, user = s.get("NowPlayingItem"), s.get("UserId")
            state = s.get("PlayState") or {}
            if not item or not item.get("Id") or not user or state.get("IsPaused"):
                continue
            playing += 1
            key = (user, s.get("DeviceId") or "", item["Id"])
            position = (state.get("PositionTicks") or 0) / 1e7
            seg = self.open.get(key)
            if seg and now - seg[1] <= SEGMENT_GAP:
                seconds = seg[2] + elapsed
                self.db.execute("UPDATE segments SET last_at = ?, seconds = ?, position = ? WHERE id = ?",
                                (now, seconds, position, seg[0]))
                self.open[key] = (seg[0], now, seconds)
            else:
                rowid = self.db.execute(
                    "INSERT INTO segments (user_id, device_id, device, client, item_id, started_at, last_at, seconds, position)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (user, key[1], s.get("DeviceName"), s.get("Client"), item["Id"], now, now, position)).lastrowid
                self.open[key] = (rowid, now, 0.0)
            self.wanted.add(item["Id"])
        self.db.commit()
        for key in [k for k, v in self.open.items() if now - v[1] > SEGMENT_GAP]:
            del self.open[key]
        self.status.update(sampledAt=datetime.fromtimestamp(now, timezone.utc).isoformat(), playing=playing)

    # --- the activity log ----------------------------------------------------------------------------------------

    async def sync(self):
        await self.sync_activity()
        await self.snapshot()
        self.status.update(syncedAt=datetime.now(timezone.utc).isoformat(), error=None)

    async def sync_activity(self):
        """New playback entries, newest first until one we have (the first run copies the whole log)."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'activity_max_id'").fetchone()
        known = int(row[0]) if row else 0
        newest, start, rows = known, 0, []
        while True:
            r = await self.client.get("/System/ActivityLog/Entries", params={"startIndex": start, "limit": PAGE})
            r.raise_for_status()
            page = r.json().get("Items") or []
            done = False
            for e in page:
                if e["Id"] <= known:
                    done = True
                    break
                newest = max(newest, e["Id"])
                if "Playback" in (e.get("Type") or ""):
                    title, device = parse_text(e.get("Name") or "")
                    rows.append((e["Id"], e["Type"], e.get("UserId"), e.get("ItemId"), parse_time(e.get("Date")),
                                 title, device, e.get("Name")))
            if done or len(page) < PAGE:
                break
            start += PAGE
        self.db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('activity_max_id', ?)", (str(newest),))
        self.db.commit()
        if rows:
            log.info("history: %d playback entries copied from the activity log", len(rows))

    async def _items(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 100):
            r = await self.client.get("/Items", params={"Ids": ",".join(ids[i:i + 100]), "Fields": ITEM_FIELDS,
                                                        "EnableImages": "false", "EnableUserData": "false"})
            r.raise_for_status()
            out += r.json().get("Items") or []
        return out

    async def snapshot(self):
        """Details of every item played that we don't have yet; gone ones matched by title."""
        have = {r[0] for r in self.db.execute("SELECT item_id FROM items")}
        ids = {r[0] for r in self.db.execute("SELECT DISTINCT item_id FROM events WHERE item_id IS NOT NULL")} | self.wanted
        ids = sorted(ids - have)
        self.wanted.clear()
        if not ids:
            return
        now = time.time()
        found = {it["Id"]: it for it in await self._items(ids)}
        for it in found.values():
            self.db.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'id', ?)",
                            (it["Id"], it.get("Type"), it.get("Name"), it.get("SeriesId"), it.get("SeriesName"),
                             it.get("ParentIndexNumber"), it.get("IndexNumber"), _runtime(it), _genres(it),
                             it.get("ProductionYear"), it["Id"], now))
        await self._series({it["SeriesId"] for it in found.values() if it.get("SeriesId")})
        gone = [i for i in ids if i not in found]
        if gone:
            await self._resolve_gone(gone, now)
        self.db.commit()
        log.info("history: %d items saved (%d by title)", len(ids), len(gone))

    async def _series(self, series_ids: set[str]):
        have = {r[0] for r in self.db.execute("SELECT series_id FROM series")}
        todo = sorted(series_ids - have)
        now = time.time()
        for s in await self._items(todo):
            self.db.execute("INSERT OR REPLACE INTO series VALUES (?, ?, ?, ?, ?, ?)",
                            (s["Id"], s.get("Name"), _genres(s), s.get("ProductionYear"), _runtime(s), now))

    async def _resolve_gone(self, gone: list[str], now: float):
        r = await self.client.get("/Items", params={"Recursive": "true", "IncludeItemTypes": "Movie,Series",
                                                    "Fields": ITEM_FIELDS, "EnableImages": "false",
                                                    "EnableUserData": "false"})
        r.raise_for_status()
        movies, series = {}, {}
        for it in r.json().get("Items") or []:
            (movies if it.get("Type") == "Movie" else series).setdefault(norm(it.get("Name")), it)
        for s in series.values():
            self.db.execute("INSERT OR IGNORE INTO series VALUES (?, ?, ?, ?, ?, ?)",
                            (s["Id"], s.get("Name"), _genres(s), s.get("ProductionYear"), _runtime(s), now))
        for item_id in gone:
            row = self.db.execute("SELECT title FROM events WHERE item_id = ? AND title IS NOT NULL ORDER BY id DESC LIMIT 1",
                                  (item_id,)).fetchone()
            title = row[0] if row else None
            hit = resolve_title(title, movies, series) if title else None
            if hit and hit[0] == "Movie":
                m = hit[1]
                values = ("Movie", m.get("Name"), None, None, None, None, _runtime(m), _genres(m), m.get("ProductionYear"),
                          m["Id"], "title")
            elif hit:
                s = hit[1]
                values = ("Episode", hit[2], s["Id"], s.get("Name"), None, None, _runtime(s), "[]", None, s["Id"], "title")
            else:
                # Not in the library any more: still counts toward hours, under its own title. "Show - Episode" is
                # kept as the show's name so its episodes group together.
                show = title.split(" - ")[0] if title and " - " in title else None
                values = ("Episode" if show else None, title, None, show, None, None, None, "[]", None, None, "missing")
            self.db.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (item_id, *values, now))

    # --- reading (in a worker thread) ----------------------------------------------------------------------------

    def load(self, since: float, until: float) -> dict:
        """Everything the year in review needs between two times, read on its own connection."""
        db = sqlite3.connect(self.path)
        try:
            pad = 86400  # a play that started just before `since` still closes inside
            events = db.execute("SELECT id, type, user_id, item_id, at, device FROM events WHERE at >= ? AND at < ? ORDER BY id",
                                (since - pad, until)).fetchall()
            segments = db.execute("SELECT user_id, item_id, device, started_at, last_at, seconds FROM segments"
                                  " WHERE last_at >= ? AND started_at < ?", (since, until)).fetchall()
            runs = db.execute("SELECT started_at, last_at FROM runs WHERE last_at >= ?", (since - pad,)).fetchall()
            items = {r[0]: r for r in db.execute("SELECT * FROM items")}
            series = {r[0]: r for r in db.execute("SELECT * FROM series")}
            seen = {(r[0], r[1]): r[2] for r in db.execute("SELECT user_id, year, state FROM seen")}
        finally:
            db.close()
        return {"events": events, "segments": segments, "runs": runs, "items": items, "series": series, "seen": seen}

    def seen(self, user_id: str, year: int) -> str | None:
        row = self.db.execute("SELECT state FROM seen WHERE user_id = ? AND year = ?", (user_id, year)).fetchone()
        return row[0] if row else None

    def mark_seen(self, user_id: str, year: int, state: str):
        # Opening it beats a dismissal; a dismissal never undoes "opened".
        if state == "dismissed" and self.seen(user_id, year) == "opened":
            return
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)", (user_id, year, state, time.time()))
        self.db.commit()

    async def users(self) -> dict[str, str]:
        r = await self.client.get("/Users")
        r.raise_for_status()
        return {u["Id"]: u["Name"] for u in r.json()}
