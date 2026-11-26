"""The year in review: each viewer's year of watching as a few slides (GET /api/wrapped), from the history in
app/history.py. It opens on WRAPPED_OPENS (Dec 1 by default), keeps counting until the year ends, and the next year
starts from nothing.

The server writes every slide's words; the apps only lay them out (kicker, big value + unit, title, caption, a short
list, a poster and a backdrop), so a new or reworded slide needs no app update.

How long a play lasted: the time the sessions sampler saw it playing when the sampler was running, else the time
between the activity log's start and stop entries, capped at 110% of the item's runtime (pauses count there)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .history import History, norm

CACHE_SECONDS = 600
# A viewer needs this much watching for a review.
MIN_SECONDS = 2 * 3600
MIN_PLAYS = 3
MATCH_PAD = 120
UNKNOWN_CAP = 3 * 3600
LIVE_CAP = 6 * 3600

GENRE_ALIASES = {
    "Action & Adventure": ["Action", "Adventure"], "Sci-Fi & Fantasy": ["Science Fiction", "Fantasy"],
    "War & Politics": ["War", "Politics"], "Game-Show": ["Game Show"], "Talk-Show": ["Talk Show"], "Talk": ["Talk Show"],
    "Sport": ["Sports"], "Kids": ["Children"], "Science-Fiction": ["Science Fiction"], "Sci-Fi": ["Science Fiction"],
}
# How a genre reads in a sentence ("12 hours of horror").
GENRE_WORDS = {"Anime": "anime", "TV Movie": "TV movies", "Documentary": "documentaries", "Mini-Series": "miniseries",
               "Children": "kids' shows", "Game Show": "game shows", "Talk Show": "talk shows", "News": "news",
               "Reality": "reality TV", "Food": "food shows", "Travel": "travel shows",
               "Home and Garden": "home and garden shows", "Sports": "sports", "Music": "music"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
DAYS = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"]
ORDINALS = {1: "number one", 2: "number two", 3: "number three"}
# Emulators and test builds: not anyone's watching.
IGNORED_DEVICES = re.compile(r"^(x86|x86_64|sdk_.*|Android SDK built for .*|sdk_google_.*)$", re.I)


def genres_of(raw: list[str]) -> list[str]:
    out = []
    for g in raw:
        for x in GENRE_ALIASES.get(g, [g]):
            if x not in out:
                out.append(x)
    return out


def genre_words(g: str) -> str:
    return GENRE_WORDS.get(g, g.lower() if g not in ("Western",) else "westerns")


# --- plays ---------------------------------------------------------------------------------------------------------

@dataclass
class Play:
    user: str
    item: str
    start: float
    seconds: float
    device: str | None = None


def pair_events(events: list[tuple]) -> list[tuple]:
    """(user, item, device, start, stop) from the log's start/stop entries (id, type, user, item, at, device), in
    order. A stop pairs with the last start of the same user, item and device; entries without a partner are dropped."""
    open_: dict[tuple, float] = {}
    out = []
    for _id, kind, user, item, at, device in events:
        if not user or not item or at is None:
            continue
        key = (user, item, device)
        if kind.endswith("PlaybackStopped"):
            start = open_.pop(key, None)
            if start is not None:
                out.append((user, item, device, start, at))
        elif kind.endswith("Playback"):
            open_[key] = at
    return out


def cap_for(info: "ItemInfo | None") -> float:
    if info and info.runtime:
        return info.runtime * 1.1
    return LIVE_CAP if info and info.kind == "TvChannel" else UNKNOWN_CAP


def assemble(pairs: list[tuple], segments: list[tuple], runs: list[tuple], infos: dict[str, "ItemInfo"]) -> list[Play]:
    """Plays with their watch time: sampled time where the sampler saw them, the log's estimate where it wasn't
    running, and sampled segments the log has no entry for (a stop that never got logged)."""
    by_key: dict[tuple, list[list]] = defaultdict(list)
    for user, item, device, started, last, seconds in segments:
        by_key[(user, item)].append([started, last, seconds, device, False])
    plays = []
    for user, item, device, start, stop in pairs:
        segs = [s for s in by_key.get((user, item), []) if not s[4] and s[0] <= stop + MATCH_PAD and s[1] >= start - MATCH_PAD]
        if segs:
            for s in segs:
                s[4] = True
            plays.append(Play(user, item, start, sum(s[2] for s in segs), device))
        elif any(r0 <= start and r1 >= stop for r0, r1 in runs):
            continue  # the sampler was watching and it never played (paused, or stopped at once)
        else:
            plays.append(Play(user, item, start, max(0.0, min(stop - start, cap_for(infos.get(item)))), device))
    for (user, item), segs in by_key.items():
        for s in segs:
            if not s[4] and s[2] > 0:
                plays.append(Play(user, item, s[0], s[2], s[3]))
    return plays


@dataclass
class ItemInfo:
    kind: str | None
    name: str | None
    show_key: str | None = None
    show_name: str | None = None
    show_id: str | None = None
    runtime: float | None = None
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    image: str | None = None  # an id that exists in the library now (movies: the movie; episodes: the show)


CHANNEL_PREFIX = re.compile(r"^[A-Z0-9]{2,4}\s?[|-] ")


def looks_like_channel(title: str | None) -> bool:
    """A gone Live TV channel or event slot by its name ("US| ESPN 2 HD", "US - FOX 5 NEW YORK", "MLB 7 | ..."):
    IPTV names are upper case after a country prefix, or carry a "|"."""
    if not title:
        return False
    if "|" in title:
        return True
    if not CHANNEL_PREFIX.match(title):
        return False
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8


def item_infos(items: dict[str, tuple], series: dict[str, tuple]) -> dict[str, ItemInfo]:
    out = {}
    for item_id, row in items.items():
        _, kind, name, series_id, series_name, _season, _episode, runtime, genres, year, image_id, resolved, _at = row
        s = series.get(series_id) if series_id else None
        if resolved == "missing" and looks_like_channel(name):
            out[item_id] = ItemInfo("TvChannel", name)
        elif kind == "Episode":
            show_name = (s[1] if s else None) or series_name
            out[item_id] = ItemInfo(kind, name, series_id or (norm(show_name) or None), show_name, series_id,
                                    runtime or (s[4] if s else None),
                                    genres_of(json.loads(s[2]) if s else []) or genres_of(json.loads(genres or "[]")),
                                    s[3] if s else None, series_id)
        else:
            out[item_id] = ItemInfo(kind, name, None, None, None, runtime, genres_of(json.loads(genres or "[]")), year,
                                    image_id)
    return out


# --- stats ---------------------------------------------------------------------------------------------------------

@dataclass
class Stats:
    seconds: float = 0
    plays: int = 0
    live: float = 0
    genres: Counter = field(default_factory=Counter)
    show_seconds: Counter = field(default_factory=Counter)
    show_episodes: dict = field(default_factory=lambda: defaultdict(set))
    item_seconds: Counter = field(default_factory=Counter)
    movie_seconds: Counter = field(default_factory=Counter)
    months: Counter = field(default_factory=Counter)
    weekdays: Counter = field(default_factory=Counter)
    night: float = 0
    days: Counter = field(default_factory=Counter)
    day_show: dict = field(default_factory=lambda: defaultdict(set))
    spans: dict = field(default_factory=lambda: defaultdict(list))
    decades: Counter = field(default_factory=Counter)
    devices: Counter = field(default_factory=Counter)
    episodes: int = 0
    movies: int = 0
    movie_times: dict = field(default_factory=dict)


def watched_enough(seconds: float, runtime: float | None) -> bool:
    return seconds >= (runtime * 0.5 if runtime else 600)


def year_stats(plays: list[Play], infos: dict[str, ItemInfo], year: int, tz: ZoneInfo) -> dict[str, Stats]:
    out: dict[str, Stats] = defaultdict(Stats)
    shows: dict[str, tuple[str, str | None]] = {}
    for p in plays:
        local = datetime.fromtimestamp(p.start, tz)
        if local.year != year or p.seconds <= 0 or (p.device and IGNORED_DEVICES.match(p.device)):
            continue
        info = infos.get(p.item) or ItemInfo(None, None)
        s = out[p.user]
        s.seconds += p.seconds
        s.plays += 1
        s.item_seconds[p.item] += p.seconds
        s.months[local.month] += p.seconds
        s.weekdays[local.weekday()] += p.seconds
        s.spans[local.date()].append((p.start, p.start + p.seconds))
        if local.hour < 5:
            s.night += p.seconds
        if p.device:
            s.devices[p.device] += p.seconds
        if info.kind == "TvChannel":
            s.live += p.seconds
            continue
        for g in info.genres:
            s.genres[g] += p.seconds
        if info.kind == "Episode" and info.show_key:
            s.show_seconds[info.show_key] += p.seconds
            s.show_episodes[info.show_key].add(p.item)
            if p.seconds >= 300:
                s.day_show[(local.date(), info.show_key)].add(p.item)
        elif info.kind == "Movie":
            key = info.image or p.item
            s.movie_seconds[key] += p.seconds
            if info.year:
                s.decades[info.year // 10 * 10] += p.seconds
    for s in out.values():
        # Time on screen per day: plays on two screens at once count once, so no day passes 24 hours.
        for d, spans in s.spans.items():
            total, end = 0.0, None
            for a, b in sorted(spans):
                if end is None or a > end:
                    total, end = total + b - a, b
                elif b > end:
                    total, end = total + b - end, b
            s.days[d] = total
        s.episodes = sum(1 for i, sec in s.item_seconds.items()
                         if (infos.get(i) and infos[i].kind == "Episode" and watched_enough(sec, infos[i].runtime)))
        runtimes = {(infos[i].image or i): infos[i].runtime for i in s.item_seconds if infos.get(i) and infos[i].kind == "Movie"}
        for key, sec in s.movie_seconds.items():
            rt = runtimes.get(key)
            if watched_enough(sec, rt):
                s.movie_times[key] = max(1, round(sec / rt)) if rt else 1
        s.movies = len(s.movie_times)
    return dict(out)


def longest_streak(days: list[date]) -> tuple[int, date | None]:
    best, run, best_end, prev = 0, 0, None, None
    for d in sorted(days):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        if run > best:
            best, best_end = run, d
        prev = d
    return best, best_end


def rank(values: dict[str, float], user: str) -> tuple[int, int]:
    """The user's place (1 = most) and how many have any."""
    ordered = sorted((v, u) for u, v in values.items() if v > 0)[::-1]
    for i, (_v, u) in enumerate(ordered):
        if u == user:
            return i + 1, len(ordered)
    return 0, len(ordered)


# --- words ---------------------------------------------------------------------------------------------------------

def hours(seconds: float) -> tuple[str, str]:
    """('1,393', 'hours'), ('2.5', 'hours'), ('45', 'minutes')."""
    h = seconds / 3600
    if h < 1:
        m = max(1, round(seconds / 60))
        return str(m), "minute" if m == 1 else "minutes"
    if h < 10:
        v = f"{h:.1f}".rstrip("0").rstrip(".")
        return v, "hour" if v == "1" else "hours"
    return f"{round(h):,}", "hours"


def hours_short(seconds: float) -> str:
    v, unit = hours(seconds)
    return f"{v} {'min' if unit.startswith('minute') else 'h'}"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def join(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def day_text(d: date) -> str:
    return f"{d.strftime('%A')}, {MONTHS[d.month - 1]} {d.day}"


def slide(id: str, **kw) -> dict:
    return {"id": id, **{k: v for k, v in kw.items() if v not in (None, "", [])}}


def build_slides(user: str, name: str, everyone: dict[str, Stats], infos: dict[str, ItemInfo], year: int,
                 show_names: dict[str, tuple[str, str | None]], movie_names: dict[str, str]) -> list[dict]:
    s = everyone[user]
    slides = []
    top_show = s.show_seconds.most_common(1)[0][0] if s.show_seconds else None
    top_show_image = show_names.get(top_show, (None, None))[1] if top_show else None

    # 1. The total.
    v, unit = hours(s.seconds)
    bits = [plural(s.episodes, "episode") if s.episodes else "", plural(s.movies, "movie") if s.movies else ""]
    days_total = s.seconds / 86400
    caption = (f"That's {days_total:.1f} days of your year" if days_total >= 1 else "") + \
              (f"{', with ' if days_total >= 1 else 'That was '}{join(bits)}." if any(bits) else ".")
    slides.append(slide("total", kicker=f"Your {year} on ClipStream", title=f"{name}, this was your year",
                        value=v, unit=f"{unit} watched", caption=caption, backdropId=top_show_image))

    # 2. Genres.
    genres = s.genres.most_common(5)
    if genres and genres[0][1] >= 3600:
        g, sec = genres[0]
        rest = [f"{genre_words(x)} ({hours_short(y)})" for x, y in genres[1:3]]
        total_g = sum(y for _x, y in genres) or 1
        slides.append(slide("genres", kicker="Your genres", title=f"{hours(sec)[0]} {hours(sec)[1]} of {genre_words(g)}",
                            caption=f"Then {join(rest)}." if rest else None,
                            items=[{"label": x, "value": hours_short(y), "share": round(y / total_g, 3)} for x, y in genres]))

    # 3. A genre crown: a genre where the viewer leads the server (comparison).
    # The biggest genre they lead, but anime when they lead that.
    crown = None
    for g, sec in s.genres.most_common():
        if sec < 3600:
            break
        place, of = rank({u: st.genres.get(g, 0) for u, st in everyone.items()}, user)
        if place == 1 and of >= 3 and (crown is None or g == "Anime"):
            crown = (g, sec, of)
    anime = s.genres.get("Anime", 0)
    if crown:
        g, sec, of = crown
        v, unit = hours(sec)
        slides.append(slide("crown", kicker="On the server", title=f"You're the server's number one {genre_words(g)} watcher",
                            value=v, unit=f"{unit} of {genre_words(g)}",
                            caption=f"More than anyone else of the {of} people who watched {genre_words(g)} here."))
    elif anime >= 3600:
        place, of = rank({u: st.genres.get("Anime", 0) for u, st in everyone.items()}, user)
        v, unit = hours(anime)
        slides.append(slide("anime", kicker="Anime", title=f"{v} {unit} of anime",
                            caption=f"That's {ORDINALS.get(place, f'number {place}')} of {of} anime watchers on the server."
                            if place and of >= 3 else None))

    # 4. The top show, then the top five.
    if top_show and s.show_seconds[top_show] >= 1800:
        sec = s.show_seconds[top_show]
        eps = len(s.show_episodes[top_show])
        v, unit = hours(sec)
        slides.append(slide("show", kicker="Your most-watched show", title=show_names[top_show][0], value=v, unit=unit,
                            caption=f"{plural(eps, 'episode')} this year.", posterId=top_show_image,
                            backdropId=top_show_image))
        top = [(k, x) for k, x in s.show_seconds.most_common(5) if x >= 1800]
        if len(top) >= 3:
            slides.append(slide("shows", kicker="Your top shows", title="The shows you kept coming back to",
                                items=[{"label": show_names[k][0], "value": hours_short(x)} for k, x in top]))

    # 5. The top movie (most times, then most time), and the next few.
    if s.movie_times:
        ordered = sorted(s.movie_times, key=lambda k: (s.movie_times[k], s.movie_seconds[k]), reverse=True)
        best = ordered[0]
        times = s.movie_times[best]
        v, unit = (str(times), "times") if times >= 2 else hours(s.movie_seconds[best])
        slides.append(slide("movie", kicker="Your most-watched movie", title=movie_names.get(best, "A movie"), value=v,
                            unit=unit, caption=f"One of {plural(s.movies, 'movie')} you watched this year."
                            if s.movies > 1 else "The one movie you watched this year.",
                            posterId=best if best in movie_names else None, backdropId=best if best in movie_names else None,
                            items=[{"label": movie_names.get(k, "?"),
                                    "value": f"{s.movie_times[k]} times" if s.movie_times[k] > 1 else hours_short(s.movie_seconds[k])}
                                   for k in ordered[1:5]]))

    # 6. The biggest binge.
    if s.day_show:
        (d, key), eps = max(s.day_show.items(), key=lambda kv: (len(kv[1]), kv[0][0]))
        if len(eps) >= 3:
            slides.append(slide("binge", kicker="Your biggest binge", value=str(len(eps)), unit="episodes in one day",
                                title=show_names[key][0], caption=f"On {day_text(d)}.",
                                backdropId=show_names[key][1]))

    # 7. Habits.
    items = []
    if s.weekdays:
        items.append({"label": "Favorite day", "value": DAYS[s.weekdays.most_common(1)[0][0]]})
    streak, _end = longest_streak([d for d, sec in s.days.items() if sec >= 300])
    if streak >= 2:
        items.append({"label": "Longest streak", "value": f"{streak} days in a row"})
    if s.days:
        d, sec = s.days.most_common(1)[0]
        items.append({"label": "Biggest day", "value": f"{MONTHS[d.month - 1][:3]} {d.day} · {hours_short(sec)}"})
    night = s.night / s.seconds if s.seconds else 0
    if night >= 0.05:
        items.append({"label": "After midnight", "value": f"{round(night * 100)}% of your watching"})
    if s.decades and s.movies >= 5:
        items.append({"label": "Movies from the", "value": f"{s.decades.most_common(1)[0][0]}s, mostly"})
    if s.live >= 3600:
        items.append({"label": "Live TV", "value": hours_short(s.live)})
    if len(s.devices) >= 2:
        items.append({"label": "Most-used screen", "value": s.devices.most_common(1)[0][0]})
    if s.months:
        m = s.months.most_common(1)[0][0]
        slides.append(slide("habits", kicker="How you watched", title=f"{MONTHS[m - 1]} was your biggest month",
                            caption=f"{hours_short(s.months[m])} that month.", items=items))

    # 8. Where you stand (comparison).
    place, of = rank({u: st.seconds for u, st in everyone.items()}, user)
    server_total = sum(st.seconds for st in everyone.values())
    if of >= 2:
        title = "You watched more than anyone on the server" if place == 1 else \
            f"You're {ORDINALS.get(place, f'number {place}')} of {of} viewers on the server"
        share = s.seconds / server_total if server_total else 0
        slides.append(slide("rank", kicker="On the server", title=title, value=str(place), unit=f"of {of}",
                            caption=f"Your hours were {round(share * 100)}% of everything watched here."
                            if share >= 0.01 else "Every hour counts."))

    # 9. Everyone together (comparison).
    viewers: Counter = Counter()
    show_total: Counter = Counter()
    for st in everyone.values():
        for k, sec in st.show_seconds.items():
            if sec >= 1800:
                viewers[k] += 1
            show_total[k] += sec
    if viewers:
        fav = max(viewers, key=lambda k: (viewers[k], show_total[k]))
        v, unit = hours(server_total)
        top = sorted(viewers, key=lambda k: (viewers[k], show_total[k]), reverse=True)[:3]
        slides.append(slide("server", kicker="Everyone together", value=v, unit=f"{unit} on the server",
                            title=f"The server's favorite show: {show_names[fav][0]}",
                            caption=f"{plural(viewers[fav], 'person', 'people')} watched it.",
                            backdropId=show_names[fav][1],
                            items=[{"label": show_names[k][0], "value": plural(viewers[k], "viewer")} for k in top]))

    slides.append(slide("outro", kicker=f"That's your {year}", title="See you next year",
                        caption="This keeps counting until December 31."))
    return slides


# --- the service ---------------------------------------------------------------------------------------------------

class Wrapped:
    def __init__(self, history: History, tz: str, opens: str, preview_all: bool):
        self.history = history
        self.tz = ZoneInfo(tz)
        self.opens = opens
        self.preview_all = preview_all
        # (when, year, the history sync it was made from, data): a sync since then (above all the first one, which
        # copies the whole log) makes it stale, or the first viewer to look would cache an empty year.
        self._cache: tuple[float, int, str | None, dict] | None = None
        self._lock = asyncio.Lock()

    def window(self, now: datetime) -> tuple[date, date]:
        month, day = (int(x) for x in self.opens.split("-"))
        return date(now.year, month, day), date(now.year + 1, 1, 1)

    def compute(self, year: int) -> dict:
        since = datetime(year, 1, 1, tzinfo=self.tz).timestamp()
        until = datetime(year + 1, 1, 1, tzinfo=self.tz).timestamp()
        data = self.history.load(since, until)
        infos = item_infos(data["items"], data["series"])
        plays = assemble(pair_events(data["events"]), data["segments"], data["runs"], infos)
        stats = year_stats(plays, infos, year, self.tz)
        show_names: dict[str, tuple[str, str | None]] = {}
        movie_names: dict[str, str] = {}
        for info in infos.values():
            if info.kind == "Episode" and info.show_key:
                show_names.setdefault(info.show_key, (info.show_name or "A show", info.show_id))
            elif info.kind == "Movie" and info.image:
                movie_names.setdefault(info.image, info.name or "A movie")
        return {"stats": stats, "infos": infos, "shows": show_names, "movies": movie_names,
                "at": datetime.now(timezone.utc).isoformat()}

    async def _data(self, year: int) -> dict:
        async with self._lock:
            synced = self.history.status.get("syncedAt")
            c = self._cache
            if c and c[1] == year and c[2] == synced and time.monotonic() - c[0] < CACHE_SECONDS:
                return c[3]
            data = await asyncio.to_thread(self.compute, year)
            self._cache = (time.monotonic(), year, synced, data)
            return data

    async def for_user(self, user_id: str, preview: bool = False) -> dict:
        now = datetime.now(self.tz)
        opens, closes = self.window(now)
        year = now.year
        base = {"year": year, "opensAt": opens.isoformat(), "closesAt": closes.isoformat()}
        if not (preview or self.preview_all or now.date() >= opens):
            return {**base, "available": False, "reason": "not_open"}
        data = await self._data(year)
        names = await self.history.users()
        # Accounts deleted since don't take part in the comparisons.
        stats = {u: st for u, st in data["stats"].items() if u in names}
        mine = stats.get(user_id)
        if not mine or mine.seconds < MIN_SECONDS or mine.plays < MIN_PLAYS:
            return {**base, "available": False, "reason": "not_enough"}
        slides = build_slides(user_id, names.get(user_id, "You"), stats, data["infos"], year, data["shows"], data["movies"])
        return {**base, "available": True, "seen": self.history.seen(user_id, year), "updatedAt": data["at"],
                "slides": slides}
