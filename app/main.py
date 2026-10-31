"""ClipStream server: Live TV (from Dispatcharr) and a release calendar (from Sonarr/Radarr) for the ClipStream app,
behind Jellyfin sign-in."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, categories
from .auth import current_user
from .calendar import Calendar
from .requests import RequestError, Seerr
from .upgrades import Upgrades
from .config import settings
from .dispatcharr import Dispatcharr
from .livetv import LiveTv
from .stalls import Stalls
from . import streams

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipstream")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jellyfin = httpx.AsyncClient(base_url=settings.jellyfin_url, timeout=20)
    # Live TV relay (signed links): a long read timeout, since a stream can pause while Dispatcharr fails over.
    app.state.relay = httpx.AsyncClient(timeout=httpx.Timeout(10, read=60), limits=httpx.Limits(max_connections=200))
    app.state.livetv = None
    app.state.stalls = None
    app.state.calendar = Calendar(settings) if settings.calendar_enabled else None
    app.state.upgrades = Upgrades(settings) if settings.calendar_enabled else None
    if app.state.upgrades:
        app.state.upgrades.start()
    app.state.seerr = Seerr(settings) if settings.requests_enabled else None
    dispatcharr = None
    if settings.livetv_enabled:
        dispatcharr = Dispatcharr(settings.dispatcharr_url, settings.dispatcharr_username, settings.dispatcharr_password)
        app.state.livetv = LiveTv(settings, dispatcharr, categories.load(settings.categories_file))
        app.state.stalls = Stalls(dispatcharr)
        await app.state.livetv.start()
    log.info("ready: livetv=%s calendar=%s requests=%s", settings.livetv_enabled, settings.calendar_enabled,
             settings.requests_enabled)
    yield
    if app.state.upgrades:
        await app.state.upgrades.stop()
    if app.state.seerr:
        await app.state.seerr.close()
    if app.state.livetv:
        await app.state.livetv.stop()
    if dispatcharr:
        await dispatcharr.close()
    await app.state.relay.aclose()
    await app.state.jellyfin.aclose()


app = FastAPI(title="ClipStream server", version=__version__, lifespan=lifespan)
# The web version of ClipStream runs on another origin. Every endpoint is authorized by the Jellyfin token in a header
# (no cookies), so any origin may call it: a page can only use a token it already has.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "X-Emby-Token", "Content-Type"])


@app.middleware("http")
async def strip_prefix(request, call_next):
    """The app looks for this server at <Jellyfin address>/clipstream. Accept that prefix whether or not the
    reverse proxy strips it."""
    path = request.scope["path"]
    if path == PREFIX or path.startswith(PREFIX + "/"):
        request.scope["path"] = path[len(PREFIX):] or "/"
    return await call_next(request)


PREFIX = "/clipstream"


def _livetv() -> LiveTv:
    if app.state.livetv is None:
        raise HTTPException(404, "Live TV isn't set up on this server")
    return app.state.livetv


@app.get("/api/health")
async def health():
    """Unauthenticated, so the app can discover the server and which features it has."""
    lt = app.state.livetv
    cal = app.state.calendar
    if cal is not None and not cal.status:
        await cal.upcoming(1, 30)  # fill in each source's status on the first check
    return {
        "name": "clipstream-server", "version": __version__,
        "features": {"livetv": lt is not None, "calendar": cal is not None, "upgrades": app.state.upgrades is not None,
                     "requests": app.state.seerr is not None},
        "livetv": {"channels": len(lt.channels), "channelsAt": lt.channels_at, "guideAt": lt.guide_at,
                   "lineup": lt.lineup.status if lt.lineup else None} if lt else None,
        "calendar": cal.status if cal else None,
    }


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return user


@app.get("/api/livetv/categories")
async def livetv_categories(user: dict = Depends(current_user)):
    return _livetv().category_list()


@app.get("/api/livetv/channels")
async def livetv_channels(category: str | None = None, user: dict = Depends(current_user)):
    return _livetv().channels_in(category)


@app.get("/api/livetv/guide")
async def livetv_guide(
    channels: str | None = Query(default=None, description="Comma-separated channel ids; all when omitted"),
    hours: int = Query(default=6, ge=1, le=48),
    user: dict = Depends(current_user),
):
    ids = {int(x) for x in channels.split(",") if x.strip().isdigit()} if channels else None
    now = datetime.now(timezone.utc)
    return _livetv().guide(ids, now - timedelta(minutes=30), now + timedelta(hours=hours))


@app.post("/api/livetv/stalls")
async def livetv_stall(report: dict = Body(...), user: dict = Depends(current_user)):
    """The app reports a Live TV stall: buffering mid-stream (how long), or a stream that failed."""
    livetv = _livetv()
    channel = next((c for c in livetv.channels if c.get("id") == report.get("channelId")), None)
    provider_name = (channel.get("effective_name") or channel.get("name")) if channel else None
    app.state.stalls.add(report, user, provider_name)
    return {"ok": True}


@app.get("/api/livetv/stalls")
async def livetv_stalls(limit: int = Query(default=50, ge=1, le=500), user: dict = Depends(current_user)):
    """Recent stalls, newest first, each with Dispatcharr's events for that channel around it."""
    _livetv()
    return await app.state.stalls.recent(limit)


@app.get("/api/livetv/stream/{uuid}")
async def livetv_stream(uuid: str, request: Request, exp: str | None = None, sig: str | None = None):
    """A channel's stream, for links handed out in the channel list. The signature stands in for the Jellyfin sign-in
    (players can't send headers), and it expires."""
    _livetv()
    secret = streams.secret_from(settings.dispatcharr_password, settings.stream_secret)
    if not settings.stream_public_url or not streams.valid(secret, uuid, exp, sig):
        raise HTTPException(403, "This Live TV link has expired; reopen the channel")
    return await streams.relay(request, app.state.relay, settings.dispatcharr_url, uuid)


@app.get("/api/livetv/logo/{logo_id}")
async def livetv_logo(logo_id: int):
    # Unauthenticated: image loaders don't carry the sign-in, and logos aren't sensitive.
    hit = await _livetv().logo(logo_id)
    if not hit:
        raise HTTPException(404)
    body, content_type = hit
    return Response(body, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/calendar")
async def calendar(
    days_back: int = Query(default=1, ge=0, le=30),
    days_ahead: int = Query(default=30, ge=1, le=120),
    user: dict = Depends(current_user),
):
    if app.state.calendar is None:
        raise HTTPException(404, "The calendar isn't set up on this server")
    return await app.state.calendar.upcoming(days_back, days_ahead)


@app.get("/api/library/upgrades")
async def library_upgrades(user: dict = Depends(current_user)):
    """Movies (TMDB ids) and episodes (TVDB ids, or series:season:episode) whose recent imports only upgraded a file
    that was already there, so the app can keep them out of Recently Added."""
    if app.state.upgrades is None:
        raise HTTPException(404, "Sonarr/Radarr aren't set up on this server")
    return await app.state.upgrades.get()


async def _seerr(user: dict) -> tuple[Seerr, int]:
    """Jellyseerr and the caller's account there."""
    seerr: Seerr | None = app.state.seerr
    if seerr is None:
        raise HTTPException(404, "Requests aren't set up on this server")
    try:
        return seerr, await seerr.user_id(user.get("id"))
    except RequestError as e:
        raise HTTPException(e.status if e.status in (403, 404) else 502, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Jellyseerr didn't answer: {e}")


async def _relay(call):
    try:
        return await call
    except RequestError as e:
        raise HTTPException(e.status if 400 <= e.status < 500 else 502, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Jellyseerr didn't answer: {e}")


@app.get("/api/requests/mine")
async def requests_mine(user: dict = Depends(current_user)):
    """The caller's requests, newest first, each with requestStatus (pending, approved, downloading, partial,
    available, declined, failed)."""
    seerr, uid = await _seerr(user)
    return await _relay(seerr.my_requests(uid))


@app.get("/api/requests/discover/{kind}")
async def requests_discover(kind: str, user: dict = Depends(current_user)):
    if kind not in ("trending", "movies", "tv", "upcoming"):
        raise HTTPException(404)
    seerr, uid = await _seerr(user)
    return await _relay(seerr.discover(uid, kind))


@app.get("/api/requests/search")
async def requests_search(q: str = Query(min_length=1), user: dict = Depends(current_user)):
    seerr, uid = await _seerr(user)
    return await _relay(seerr.search(uid, q))


@app.get("/api/requests/{media_type}/{tmdb_id}")
async def requests_title(media_type: str, tmdb_id: int, user: dict = Depends(current_user)):
    """One title; a show lists its seasons with each one's status, so the app can offer what's left."""
    if media_type not in ("movie", "tv"):
        raise HTTPException(404)
    seerr, uid = await _seerr(user)
    return await _relay(seerr.title(uid, media_type, tmdb_id))


@app.post("/api/requests/{media_type}/{tmdb_id}")
async def requests_create(media_type: str, tmdb_id: int, seasons: list[int] | None = Body(default=None, embed=True),
                          user: dict = Depends(current_user)):
    """Request a movie, or a show's `seasons` (all of them when left out), as the caller."""
    if media_type not in ("movie", "tv"):
        raise HTTPException(404)
    seerr, uid = await _seerr(user)
    return await _relay(seerr.request(uid, media_type, tmdb_id, seasons))
