"""Real game times from ESPN's public scoreboards, so event slots only show while there's something to watch.

Providers keep game slots up with an "event starting soon" picture for hours before a game, after it, and on slots
whose names are left over from another day. With the real schedule, a slot shows from PREGAME before the start until
POSTGAME after ESPN calls it final; overtime and extra innings need nothing special (the game stays in progress). A
league slot whose teams aren't on that league's schedule at all (yesterday, today, tomorrow) is a leftover and hidden.
Events ESPN doesn't cover (UFC, F1, boxing...) keep the provider's times, with the same pregame cutoff and an hour
after their listed end."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .events import Event

log = logging.getLogger("clipstream.scores")
UTC = timezone.utc
BASE = "https://site.api.espn.com/apis/site/v2/sports"
REFRESH_SECONDS = 300
GAME_LENGTH = timedelta(hours=3)
# Unmatched events: how long after their listed (or assumed) end to keep them.
FALLBACK_POSTGAME = timedelta(hours=1)
# A league slot is a leftover when neither team plays within this long of its listed time.
LEFTOVER_WINDOW = timedelta(hours=8)

# Slot league -> ESPN scoreboards. Leagues marked strict hide slots whose teams aren't on the schedule at all.
LEAGUES: dict[str, list[str]] = {
    "NHL": ["hockey/nhl"],
    "NBA": ["basketball/nba"],
    "WNBA": ["basketball/wnba"],
    "NFL": ["football/nfl"],
    "MLB": ["baseball/mlb"],
    "NCAAF": ["football/college-football?groups=80", "football/college-football?groups=81"],
    "NCAAB": ["basketball/mens-college-basketball?groups=50"],
    "SOCCER": ["soccer/usa.1", "soccer/eng.1", "soccer/uefa.champions", "soccer/uefa.europa", "soccer/esp.1",
               "soccer/ita.1", "soccer/ger.1", "soccer/fra.1", "soccer/bra.1", "soccer/fifa.world"],
}
STRICT = {"NHL", "NBA", "WNBA", "NFL", "MLB", "NCAAF", "NCAAB"}
HINTS = [
    (re.compile(r"\bWNBA\b", re.I), "WNBA"),
    (re.compile(r"\bNBA\b", re.I), "NBA"),
    (re.compile(r"\bNHL\b", re.I), "NHL"),
    (re.compile(r"\bNFL\b", re.I), "NFL"),
    (re.compile(r"\bMLB\b", re.I), "MLB"),
    (re.compile(r"\bNCAAF\b|college football", re.I), "NCAAF"),
    (re.compile(r"\bNCAAB\b|college basketball", re.I), "NCAAB"),
    (re.compile(r"\b(MLS|EPL|premier league|champions league|europa league|la liga|serie a|bundesliga|ligue 1"
                r"|brasileir[aã]o)\b", re.I), "SOCCER"),
]


def league_of(name: str) -> str | None:
    for pattern, league in HINTS:
        if pattern.search(name):
            return league
    return None


def norm(text: str) -> str:
    """Lowercase words with single spaces and a space at each end, for whole-word matching."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text.lower().replace("&", " and "))
    return f" {text.strip()} "


# Abbreviations that are also everyday words in slot names.
_ABBR_WORDS = {"at", "vs", "no", "in", "on", "of", "or", "an", "la"}
MATCHUP = re.compile(r"\s(@|at|vs\.?|v|x)\s", re.I)


def team_names(team: dict) -> list[str]:
    names = {team.get("displayName"), team.get("shortDisplayName"), team.get("location")}
    if len(team.get("name") or "") >= 4:  # nicknames: "Devils", but not "49ers"-style clashes under 4 letters
        names.add(team.get("name"))
    out = []
    for n in names:
        if n and len(n) >= 3 and (k := norm(n)) not in out:
            out.append(k)
    abbr = (team.get("abbreviation") or "").lower()
    if len(abbr) >= 2 and abbr not in _ABBR_WORDS and f" {abbr} " not in out:
        out.append(f" {abbr} ")  # "TB x NYY"
    return out


@dataclass
class Game:
    id: str
    league: str
    start: datetime
    state: str  # pre | in | post | off (postponed, canceled)
    teams: list[list[str]]
    ended_at: datetime | None = None

    def matched(self, title: str) -> int:
        return sum(1 for names in self.teams if any(n in title for n in names))


def parse_scoreboard(league: str, data: dict) -> list[Game]:
    games = []
    for e in data.get("events", []):
        try:
            start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            st = e["status"]["type"]
            competitors = e["competitions"][0]["competitors"]
        except (KeyError, IndexError, ValueError):
            continue
        state = st.get("state", "pre")
        if st.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED", "STATUS_FORFEIT"):
            state = "off"
        games.append(Game(id=str(e.get("id")), league=league, start=start, state=state,
                          teams=[team_names(c.get("team") or {}) for c in competitors]))
    return games


@dataclass
class Match:
    game: Game | None = None
    missing: bool = False  # a strict league slot whose teams aren't on the schedule: a leftover


def find(name: str, title: str, provider_start: datetime | None, games: list[Game], loaded: set[str],
         now: datetime) -> Match:
    league = league_of(name)
    pool = [g for g in games if league is None or g.league == league]
    t = norm(title)
    when = provider_start or now
    both = [g for g in pool if g.matched(t) >= 2]
    if both:
        return Match(min(both, key=lambda g: abs(g.start - when)))
    if league is None:
        return Match()
    # One team: provider typos ("lowa"), or names the scoreboard spells differently. Only for a matchup with a listed
    # time, and not when the other name is a team of its own ("North Carolina at Charlotte" isn't Tulane at Charlotte;
    # "USA Network" isn't South Alabama).
    if provider_start and MATCHUP.search(title):
        one = [g for g in pool if g.matched(t) == 1 and abs(g.start - provider_start) <= timedelta(hours=3)]
        one = [g for g in one if not any(o.matched(t) for o in pool if o.id != g.id and not _same_teams(o, g, t))]
        if len(one) == 1:
            return Match(one[0])
    # Only a slot that names a matchup ("Devils @ Rangers") can be a leftover; "NHL GAME 01" alone says nothing. A team
    # playing another day (NC State tomorrow) doesn't make tonight's "NC State at Wake Forest" real.
    near = [g for g in pool if abs(g.start - when) <= LEFTOVER_WINDOW]
    missing = (league in STRICT and league in loaded and bool(MATCHUP.search(title))
               and not any(g.matched(t) for g in near))
    return Match(missing=missing)


def _same_teams(a: Game, b: Game, title: str) -> bool:
    """Whether the names `a` matches in the title all belong to b's teams (the same team on another day)."""
    names = {n for team in a.teams for n in team if n in title}
    return all(any(n in team for team in b.teams) for n in names)


def apply(event: Event, match: Match, now: datetime, pregame: timedelta, postgame: timedelta) -> bool:
    """Puts the real times on the event; False when the slot shouldn't be shown now."""
    g = match.game
    if match.missing:
        return False
    if g is None:
        if event.status == "upcoming":
            return event.start is not None and now >= event.start - pregame
        if event.status == "ended":
            if event.end and now < event.end + FALLBACK_POSTGAME:
                event.status = "live"  # listed end times are guesses (3 h); the event may well still be on
                return True
            return False
        return True
    if g.state == "off":
        return False
    event.start = g.start
    if g.state == "pre":
        event.end = g.start + GAME_LENGTH
        event.status = "upcoming" if now < g.start else "live"  # past the start but not begun: a delay
        return now >= g.start - pregame
    if g.state == "in":
        event.end = max(g.start + GAME_LENGTH, now + timedelta(minutes=15))
        event.status = "live"
        return True
    ended = g.ended_at or now
    event.end = ended
    event.status = "live"  # postgame show
    return now < ended + postgame


@dataclass
class Scoreboard:
    local_timezone: str
    games: list[Game] = field(default_factory=list)
    loaded: set[str] = field(default_factory=set)
    _ended: dict[str, datetime] = field(default_factory=dict)
    _task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("scoreboard refresh failed")
            await asyncio.sleep(REFRESH_SECONDS)

    async def refresh(self):
        now = datetime.now(UTC)
        today = now.astimezone(ZoneInfo(self.local_timezone)).date()
        days = [(today + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]
        jobs = [(league, path, day) for league, paths in LEAGUES.items() for path in paths for day in days]
        sem = asyncio.Semaphore(6)
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "clipstream-server"}) as client:
            async def one(league: str, path: str, day: str):
                async with sem:
                    league_path, _, extra = path.partition("?")
                    r = await client.get(f"{BASE}/{league_path}/scoreboard?dates={day}&limit=500" + (f"&{extra}" if extra else ""))
                    r.raise_for_status()
                    return league, parse_scoreboard(league, r.json())
            results = await asyncio.gather(*(one(*j) for j in jobs), return_exceptions=True)
        games, failed = {}, set()
        for (league, _, _), res in zip(jobs, results):
            if isinstance(res, BaseException):
                failed.add(league)
                continue
            for g in res[1]:
                games[g.id] = g
        for g in games.values():
            if g.state == "post":
                # First seen final now; a game that ended long before this server started gets an estimate.
                guess = now if now - g.start < GAME_LENGTH + timedelta(hours=2) else g.start + GAME_LENGTH
                g.ended_at = self._ended.setdefault(g.id, guess)
        self.games = list(games.values())
        # "Not on the schedule" only counts for leagues whose every scoreboard loaded.
        self.loaded = {league for league in LEAGUES if league not in failed}
        if failed:
            log.warning("scoreboards failed for %s", ", ".join(sorted(failed)))
        log.info("scoreboard: %d games", len(self.games))

    def find(self, name: str, title: str, provider_start: datetime | None, now: datetime) -> Match:
        if not self.games and not self.loaded:
            return Match()
        return find(name, title, provider_start, self.games, self.loaded, now)
