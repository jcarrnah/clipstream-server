"""Live TV stalls reported by the apps (buffering mid-stream, or a stream that failed), kept in memory and matched with
Dispatcharr's own events for that channel (provider too slow, failover to a backup stream, reconnects), so "why did it
buffer" can be answered after the fact: GET /api/livetv/stalls."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from .dispatcharr import Dispatcharr

log = logging.getLogger("clipstream.stalls")

KEEP = 500
EVENT_TYPES = {"channel_buffering", "stream_switch", "channel_failover", "channel_reconnect", "channel_error",
               "stream_error", "channel_start", "channel_stop"}


def _parse(ts: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _event_view(e: dict) -> dict:
    d = e.get("details") or {}
    keep = ("speed", "reason", "stream_name", "previous_stream_name", "previous_provider_name", "attempt", "error")
    return {"at": e.get("timestamp"), "type": e.get("event_type"), **{k: d[k] for k in keep if k in d}}


def matching(stall: dict, events: list[dict]) -> list[dict]:
    """Dispatcharr's events for the stall's channel from a minute before it to half a minute after it ended."""
    start = _parse(stall["startedAt"])
    if start is None:
        return []
    end = start + timedelta(seconds=stall.get("seconds") or 0)
    # Dispatcharr logs the provider's channel name; the apps send the tidied one shown to viewers.
    name = " ".join((stall.get("providerName") or stall.get("channel") or "").upper().split())
    out = []
    for e in events:
        at = _parse(e.get("timestamp", ""))
        if (e.get("event_type") in EVENT_TYPES and at and start - timedelta(seconds=60) <= at <= end + timedelta(seconds=30)
                and " ".join((e.get("channel_name") or "").upper().split()) == name):
            out.append(_event_view(e))
    return sorted(out, key=lambda v: v["at"])


class Stalls:
    def __init__(self, dispatcharr: Dispatcharr):
        self.dispatcharr = dispatcharr
        self.items: deque[dict] = deque(maxlen=KEEP)

    def add(self, report: dict, user: dict, provider_name: str | None = None) -> dict:
        stall = {
            "startedAt": str(report.get("startedAt") or datetime.now(timezone.utc).isoformat()),
            "seconds": round(float(report.get("seconds") or 0), 1),
            "kind": str(report.get("kind") or "buffering")[:20],
            "channel": str(report.get("channel") or "")[:200],
            "channelId": report.get("channelId"),
            "providerName": provider_name,
            "ended": bool(report.get("ended", True)),
            "detail": str(report.get("detail") or "")[:300] or None,
            "video": str(report.get("video") or "")[:100] or None,
            "device": str(report.get("device") or "")[:100] or None,
            "user": user.get("name"),
        }
        self.items.append(stall)
        log.info("stall: %s %ss on %s (%s, %s)%s", stall["kind"], stall["seconds"], stall["channel"], stall["user"],
                 stall["device"], f" {stall['detail']}" if stall["detail"] else "")
        return stall

    async def recent(self, limit: int) -> list[dict]:
        stalls = list(self.items)[-limit:][::-1]
        if not stalls:
            return []
        try:
            events = await self.dispatcharr.system_events(1000)
        except Exception:
            log.exception("couldn't read Dispatcharr's events")
            events = []
        return [{**s, "dispatcharr": matching(s, events)} for s in stalls]
