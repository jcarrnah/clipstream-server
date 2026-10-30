"""Signed Live TV links. Dispatcharr's own stream URLs (`/proxy/ts/stream/<uuid>`) play for anyone who has one, forever.
With STREAM_PUBLIC_URL set, channel lists hand out links to this server instead, signed and good for LINK_HOURS; the
server checks the signature and relays the stream from Dispatcharr over the LAN. Dispatcharr's /proxy/ can then be
closed to the internet. The relay adds a hop inside the same machine, not a trip through Cloudflare: point
STREAM_PUBLIC_URL at a DNS-only hostname, like the Dispatcharr one was."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("clipstream.streams")

LINK_HOURS = 12


def secret_from(password: str | None, explicit: str | None = None) -> bytes:
    """STREAM_SECRET if set, else derived from the Dispatcharr password: stable across restarts, nothing new to set."""
    base = explicit or f"clipstream-stream:{password or ''}"
    return hashlib.sha256(base.encode()).digest()


def sign(secret: bytes, uuid: str, expires: int) -> str:
    return hmac.new(secret, f"{uuid}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_url(public_base: str, secret: bytes, uuid: str, now: float | None = None) -> str:
    # Rounded to the hour, so a channel's link stays the same between refreshes (players and caches see one URL).
    hour = int((now if now is not None else time.time()) // 3600) * 3600
    expires = hour + LINK_HOURS * 3600
    return f"{public_base}/api/livetv/stream/{uuid}?exp={expires}&sig={sign(secret, uuid, expires)}"


def valid(secret: bytes, uuid: str, exp: str | None, sig: str | None, now: float | None = None) -> bool:
    if not exp or not sig or not exp.isdigit():
        return False
    if int(exp) < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(sign(secret, uuid, int(exp)), sig)


async def relay(request: Request, client: httpx.AsyncClient, dispatcharr_url: str, uuid: str) -> StreamingResponse:
    """Plays Dispatcharr's stream for the caller. Closing the player closes the upstream connection, so Dispatcharr
    lets go of the provider connection as it did before."""
    headers = {"User-Agent": request.headers.get("user-agent", "ClipStream")}
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    upstream_request = client.build_request("GET", f"{dispatcharr_url}/proxy/ts/stream/{uuid}", headers=headers)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as e:
        log.warning("stream %s: Dispatcharr didn't answer (%s)", uuid, e)
        raise HTTPException(502, "Live TV isn't answering right now")
    if upstream.status_code != 200:
        await upstream.aclose()
        raise HTTPException(upstream.status_code if upstream.status_code in (404, 503) else 502, "That channel isn't available right now")

    async def body():
        try:
            # As it arrives: a chunk size would hold data back until that much had come in.
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), media_type=upstream.headers.get("content-type", "video/mp2t"),
                             headers={"Cache-Control": "no-store"})
