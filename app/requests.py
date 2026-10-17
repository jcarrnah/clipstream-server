"""Requests: Jellyseerr behind the app's own sign-in.

The server holds Jellyseerr's API key and acts as the signed-in Jellyfin user (Jellyseerr's `X-Api-User` header), so
requests carry the right name and approval rules, nobody signs in twice, and the key never reaches a device. A Jellyfin
user who never opened Jellyseerr is imported on first use, as Jellyseerr does at a first sign-in.

Everything the app gets is flattened into one item shape (see `_item`), the same for search, discover and requests."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from .config import Settings

log = logging.getLogger("clipstream.requests")
POSTER = "https://image.tmdb.org/t/p/w342"
BACKDROP = "https://image.tmdb.org/t/p/w780"
USERS_SECONDS = 600
DETAILS_SECONDS = 3600

# Jellyseerr's MediaStatus and MediaRequestStatus.
MEDIA_STATUS = {2: "pending", 3: "processing", 4: "partial", 5: "available"}
REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DECLINED, REQUEST_FAILED, REQUEST_COMPLETED = 1, 2, 3, 4, 5


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _download(media: dict) -> dict | None:
    """Progress of what Radarr/Sonarr are downloading for this title, when Jellyseerr knows it."""
    items = [d for d in media.get("downloadStatus") or [] if d.get("size")]
    if not items:
        return None
    size = sum(d["size"] for d in items)
    left = sum(d.get("sizeLeft") or 0 for d in items)
    eta = max((d.get("estimatedCompletionTime") for d in items if d.get("estimatedCompletionTime")), default=None)
    return {"progress": round(1 - left / size, 3) if size else 0.0, "eta": eta}


def _item(r: dict, media_type: str | None = None) -> dict:
    """A movie or show from Jellyseerr (search, discover or details) in the app's shape."""
    media = r.get("mediaInfo") or {}
    kind = media_type or r.get("mediaType")
    return {
        "type": kind,
        "tmdbId": r.get("id"),
        "title": r.get("title") or r.get("name") or "",
        "year": _year(r.get("releaseDate") or r.get("firstAirDate")),
        "overview": r.get("overview") or "",
        "poster": POSTER + r["posterPath"] if r.get("posterPath") else None,
        "backdrop": BACKDROP + r["backdropPath"] if r.get("backdropPath") else None,
        "status": MEDIA_STATUS.get(media.get("status")),
        "jellyfinId": media.get("jellyfinMediaId"),
        "download": _download(media),
    }


def request_status(request: dict) -> str:
    """One word for a request: declined, failed, available, partial, downloading, approved or pending."""
    media = request.get("media") or {}
    if request.get("status") == REQUEST_DECLINED:
        return "declined"
    if request.get("status") == REQUEST_FAILED:
        return "failed"
    if media.get("status") == 5:
        return "available"
    if media.get("status") == 4:
        return "partial"
    if request.get("status") == REQUEST_PENDING:
        return "pending"
    if _download(media):
        return "downloading"
    return "approved"


class Seerr:
    def __init__(self, settings: Settings):
        self.client = httpx.AsyncClient(
            base_url=settings.seerr_url + "/api/v1", timeout=20, headers={"X-Api-Key": settings.seerr_api_key or ""})
        self._users: tuple[float, dict[str, int]] | None = None
        self._users_lock = asyncio.Lock()
        self._details: dict[tuple[str, int], tuple[float, dict]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _call(self, method: str, path: str, user: int | None = None, **kwargs):
        headers = {"X-Api-User": str(user)} if user is not None else {}
        r = await self.client.request(method, path, headers=headers, **kwargs)
        if r.status_code >= 400:
            try:
                message = r.json().get("message") or r.text
            except ValueError:
                message = r.text
            raise RequestError(r.status_code, message or f"Jellyseerr answered {r.status_code}")
        return r.json() if r.content else None

    # Users

    async def _user_map(self, refresh: bool = False) -> dict[str, int]:
        """Jellyfin user id (no dashes, lower case) -> Jellyseerr user id."""
        if not refresh and self._users and time.monotonic() - self._users[0] < USERS_SECONDS:
            return self._users[1]
        users = await self._call("GET", "/user", params={"take": 1000})
        mapping = {}
        for u in users.get("results") or []:
            if u.get("jellyfinUserId"):
                mapping[u["jellyfinUserId"].replace("-", "").lower()] = u["id"]
        self._users = (time.monotonic(), mapping)
        return mapping

    async def user_id(self, jellyfin_user_id: str | None) -> int:
        if not jellyfin_user_id:
            raise RequestError(403, "Requests need a Jellyfin user sign-in")
        key = jellyfin_user_id.replace("-", "").lower()
        found = (await self._user_map()).get(key)
        if found is not None:
            return found
        async with self._users_lock:
            found = (await self._user_map(refresh=True)).get(key)
            if found is None:
                log.info("importing Jellyfin user %s into Jellyseerr", key)
                await self._call("POST", "/user/import-from-jellyfin", json={"jellyfinUserIds": [key]})
                found = (await self._user_map(refresh=True)).get(key)
        if found is None:
            raise RequestError(403, "Jellyseerr has no account for this user")
        return found

    # Titles

    async def details(self, media_type: str, tmdb_id: int, fresh: bool = False) -> dict:
        """Jellyseerr's movie or show page (TMDB data plus what Jellyseerr knows of it), cached for titles' names."""
        key = (media_type, tmdb_id)
        hit = self._details.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < DETAILS_SECONDS:
            return hit[1]
        data = await self._call("GET", f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}")
        self._details[key] = (time.monotonic(), data)
        return data

    async def search(self, user: int, query: str) -> list[dict]:
        # Jellyseerr wants spaces as %20 (a "+" is searched for literally).
        data = await self._call("GET", f"/search?query={quote(query, safe='')}&page=1", user)
        return [_item(r) for r in data.get("results") or [] if r.get("mediaType") in ("movie", "tv")]

    async def discover(self, user: int, kind: str) -> list[dict]:
        path, media_type = {
            "trending": ("/discover/trending", None),
            "movies": ("/discover/movies", "movie"),
            "tv": ("/discover/tv", "tv"),
            "upcoming": ("/discover/movies/upcoming", "movie"),
        }[kind]
        pages = await asyncio.gather(*(self._call("GET", path, user, params={"page": p}) for p in (1, 2)))
        out, seen = [], set()
        for page in pages:
            for r in page.get("results") or []:
                item = _item(r, media_type)
                if item["type"] in ("movie", "tv") and (item["type"], item["tmdbId"]) not in seen:
                    seen.add((item["type"], item["tmdbId"]))
                    out.append(item)
        return out

    async def title(self, user: int, media_type: str, tmdb_id: int) -> dict:
        """A title with what this user may still request: for a show, each season and its state."""
        data = await self.details(media_type, tmdb_id, fresh=True)
        item = _item(data, media_type)
        if media_type == "tv":
            media = data.get("mediaInfo") or {}
            have = {s["seasonNumber"]: MEDIA_STATUS.get(s.get("status")) for s in media.get("seasons") or []}
            requested = set()
            for req in media.get("requests") or []:
                if req.get("status") not in (REQUEST_DECLINED, REQUEST_FAILED):
                    requested.update(s["seasonNumber"] for s in req.get("seasons") or [])
            item["seasons"] = [
                {
                    "number": s["seasonNumber"],
                    "name": s.get("name") or f"Season {s['seasonNumber']}",
                    "episodes": s.get("episodeCount") or 0,
                    "status": have.get(s["seasonNumber"]) or ("pending" if s["seasonNumber"] in requested else None),
                }
                for s in data.get("seasons") or [] if s.get("seasonNumber", 0) > 0
            ]
        return item

    # Requests

    async def my_requests(self, user: int) -> list[dict]:
        data = await self._call("GET", "/request", user, params={
            "take": 100, "skip": 0, "filter": "all", "sort": "added", "requestedBy": user})
        requests = data.get("results") or []

        async def one(req: dict) -> dict | None:
            media = req.get("media") or {}
            kind, tmdb = req.get("type") or media.get("mediaType"), media.get("tmdbId")
            if kind not in ("movie", "tv") or not tmdb:
                return None
            try:
                details = await self.details(kind, tmdb)
            except RequestError as e:
                log.warning("details %s %s: %s", kind, tmdb, e)
                details = {"id": tmdb}
            item = _item({**details, "mediaInfo": media}, kind)
            item.update({
                "requestId": req.get("id"),
                "requestStatus": request_status(req),
                "requestedAt": req.get("createdAt"),
                "requestedSeasons": [s["seasonNumber"] for s in req.get("seasons") or []] if kind == "tv" else None,
            })
            return item

        items = await asyncio.gather(*(one(r) for r in requests))
        return [i for i in items if i]

    async def request(self, user: int, media_type: str, tmdb_id: int, seasons: list[int] | None) -> dict:
        if media_type == "movie":
            # Jellyseerr takes a second request for a movie it already has (and approves it again).
            known = (await self.details("movie", tmdb_id, fresh=True)).get("mediaInfo") or {}
            if MEDIA_STATUS.get(known.get("status")):
                raise RequestError(409, "This movie is already requested or in the library")
        body: dict = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            body["seasons"] = seasons or "all"
        created = await self._call("POST", "/request", user, json=body)
        self._details.pop((media_type, tmdb_id), None)
        return {"requestId": (created or {}).get("id"), "requestStatus": request_status(created or {})}
