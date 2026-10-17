"""Callers authenticate with the Jellyfin token their app already has. We ask Jellyfin who it belongs to and
cache the answer briefly, so disabling a user in Jellyfin cuts them off here within minutes."""
from __future__ import annotations

import re
import time

import httpx
from fastapi import Header, HTTPException, Request

CACHE_SECONDS = 300
_cache: dict[str, tuple[float, dict]] = {}


def _token(authorization: str | None, emby_token: str | None, api_key: str | None) -> str | None:
    if emby_token:
        return emby_token
    if authorization:
        m = re.search(r'Token="([^"]+)"', authorization)
        if m:
            return m.group(1)
    return api_key


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_emby_token: str | None = Header(default=None),
) -> dict:
    token = _token(authorization, x_emby_token, request.query_params.get("api_key"))
    if not token:
        raise HTTPException(401, "Sign in to Jellyfin first")
    now = time.monotonic()
    hit = _cache.get(token)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    jellyfin: httpx.AsyncClient = request.app.state.jellyfin
    headers = {"Authorization": f'MediaBrowser Token="{token}"'}
    r = await jellyfin.get("/Users/Me", headers=headers)
    if r.status_code in (401, 403):
        _cache.pop(token, None)
        raise HTTPException(401, "Jellyfin didn't accept this sign-in")
    if r.is_success:
        me = r.json()
        user = {"id": me.get("Id"), "name": me.get("Name"), "admin": bool((me.get("Policy") or {}).get("IsAdministrator"))}
    else:
        # Server API keys aren't a user, so /Users/Me fails; accept them if Jellyfin does (admin-created keys).
        info = await jellyfin.get("/System/Info", headers=headers)
        if not info.is_success:
            raise HTTPException(401, "Jellyfin didn't accept this sign-in")
        user = {"id": None, "name": "api-key", "admin": True}
    _cache[token] = (now, user)
    return user
