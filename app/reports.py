"""Problem reports from the apps ("this file won't play", "the audio is out of sync"), with the viewer's own words:
kept (in memory, and in REPORTS_FILE when set), logged, and posted to a Discord channel through a webhook so the
server's owner hears about them. GET /api/reports lists them for admins."""
from __future__ import annotations

import json
import re
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("clipstream.reports")

KEEP = 500
# A viewer can't flood the channel: this many reports per user per hour.
PER_HOUR = 10
KINDS = {
    "wont_play": "Won't play",
    "stops": "Stops or buffers partway",
    "video": "Picture problem",
    "audio": "Sound problem (missing, out of sync, wrong language)",
    "subtitles": "Subtitle problem",
    "wrong": "Wrong file or episode",
    "other": "Something else",
}


def _clip(value, n: int) -> str | None:
    s = str(value or "").strip()
    return s[:n] or None


def item_title(item: dict) -> str:
    """'Show S1E2 · Episode' or 'Movie (Year)', from a Jellyfin item."""
    if item.get("Type") == "Episode":
        number = ""
        if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
            number = f" S{item['ParentIndexNumber']}E{item['IndexNumber']}"
        return f"{item.get('SeriesName') or ''}{number} · {item.get('Name') or ''}".strip(" ·")
    year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    return f"{item.get('Name') or 'Unknown'}{year}"


def file_facts(item: dict) -> str | None:
    """Container, size, video and audio of the item's first source: what's needed to tell a broken file from a
    playback problem."""
    sources = item.get("MediaSources") or []
    if not sources:
        return "Jellyfin lists no media source for this item"
    s = sources[0]
    parts = []
    if s.get("Container"):
        parts.append(s["Container"])
    if s.get("Size"):
        parts.append(f"{s['Size'] / 1024 ** 3:.2f} GB")
    streams = s.get("MediaStreams") or []
    video = next((m for m in streams if m.get("Type") == "Video"), None)
    if video:
        parts.append(" ".join(str(x) for x in (video.get("Codec"), f"{video.get('Width')}x{video.get('Height')}",
                                                  video.get("VideoRangeType")) if x))
    else:
        parts.append("no video stream")
    audio = [m for m in streams if m.get("Type") == "Audio"]
    if audio:
        parts.append(f"{len(audio)} audio ({', '.join(sorted({str(a.get('Language') or '?') for a in audio}))})")
    subs = [m for m in streams if m.get("Type") == "Subtitle"]
    if subs:
        parts.append(f"{len(subs)} subtitles")
    if s.get("Path"):
        parts.append(s["Path"])
    return " · ".join(parts)


def discord_payload(report: dict, mention: str | None, link: str | None) -> dict:
    """A webhook message: the mention (so it pings), and an embed with the title, the viewer's words and the facts."""
    fields = [
        {"name": "Problem", "value": KINDS.get(report["kind"], report["kind"]), "inline": True},
        {"name": "From", "value": f"{report['user']} · {report.get('app') or '?'}", "inline": True},
    ]
    if report.get("position"):
        fields.append({"name": "At", "value": report["position"], "inline": True})
    if report.get("device"):
        fields.append({"name": "Device", "value": report["device"], "inline": True})
    if report.get("playback"):
        fields.append({"name": "Playback", "value": report["playback"][:1000], "inline": False})
    if report.get("file"):
        fields.append({"name": "File", "value": report["file"][:1000], "inline": False})
    embed = {
        "title": f"Problem report: {report['title']}"[:256],
        "description": report.get("message") or "(no message)",
        "color": 0xF85149,
        "fields": fields,
        "timestamp": report["at"],
    }
    if link:
        embed["url"] = link
    return {"content": mention or None, "embeds": [embed], "allowed_mentions": {"parse": ["users", "roles"]}}


_WEBHOOK = re.compile(r"((?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+)")


def webhook_url(value: str | None) -> str | None:
    """The webhook as pasted into a container setting. Its address is found anywhere in the value, so quotes, a
    missing https://, invisible characters copied along with it (seen on Unraid) or just "<id>/<token>" all work;
    anything else is refused with a log line saying so."""
    v = (value or "").strip()
    if not v:
        return None
    m = _WEBHOOK.search(v)
    if m:
        url = "https://" + m.group(1)
    elif (other := re.search(r"https?://\S+", v)):
        url = other.group(0)  # another service that takes Discord's format
    else:
        bare = re.search(r"(\d{5,})/([\w-]{20,})", v)
        url = f"https://discord.com/api/webhooks/{bare.group(1)}/{bare.group(2)}" if bare else None
    if not url:
        log.warning("DISCORD_REPORT_WEBHOOK isn't a webhook URL (it should look like "
                    "https://discord.com/api/webhooks/<id>/<token>); reports won't reach Discord")
        return None
    if not v.startswith(url):
        log.info("DISCORD_REPORT_WEBHOOK had %d extra character(s) around the address; using the address", len(v) - len(url))
    log.info("Reports go to a Discord webhook (%s/…)", url.rsplit("/", 1)[0])
    return url


def mention_text(value: str | None) -> str | None:
    """Who to ping: a user id, "@<id>" or "<@id>" become Discord's <@id>; roles (<@&id>) and anything else as given."""
    v = (value or "").strip().strip("'\"").strip()
    m = re.fullmatch(r"<?@?!?(\d{5,})>?", v)
    return f"<@{m.group(1)}>" if m else (v or None)


class Reports:
    def __init__(self, webhook: str | None, mention: str | None, public_url: str | None, path: str | None):
        self.webhook = webhook_url(webhook)
        self.mention = mention_text(mention)
        self.public_url = public_url
        self.path = Path(path) if path else None
        self.items: deque[dict] = deque(maxlen=KEEP)
        self.sent: dict[str, list[float]] = {}
        self.http = httpx.AsyncClient(timeout=10)
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines()[-KEEP:]:
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    pass

    async def close(self):
        await self.http.aclose()

    def allowed(self, user_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        recent = [t for t in self.sent.get(user_id, []) if now - t < 3600]
        self.sent[user_id] = recent
        return len(recent) < PER_HOUR

    def build(self, body: dict, user: dict, item: dict | None) -> dict:
        kind = body.get("kind") if body.get("kind") in KINDS else "other"
        seconds = body.get("positionSeconds")
        position = None
        if isinstance(seconds, (int, float)) and seconds > 0:
            s = int(seconds)
            position = f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
        return {
            "id": uuid.uuid4().hex[:12],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "itemId": _clip(body.get("itemId"), 64),
            "title": item_title(item) if item else (_clip(body.get("title"), 200) or "Unknown item"),
            "kind": kind,
            "message": _clip(body.get("message"), 1500),
            "position": position,
            "app": _clip(body.get("app"), 60),
            "device": _clip(body.get("device"), 100),
            "playback": _clip(body.get("playback"), 500),
            "file": file_facts(item) if item else None,
            "user": user.get("name") or "?",
            "userId": user.get("id"),
            "resolved": False,
        }

    def link(self, report: dict) -> str | None:
        if not self.public_url or not report.get("itemId"):
            return None
        return f"{self.public_url}/web/#/details?id={report['itemId']}"

    async def add(self, report: dict) -> dict:
        self.items.append(report)
        self.sent.setdefault(report["userId"] or report["user"], []).append(time.time())
        log.info("report: %s: %s (%s, %s) %s", report["title"], report["kind"], report["user"], report["app"],
                 report.get("message") or "")
        if self.path:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(report) + "\n")
            except OSError as e:
                log.warning("couldn't keep the report in %s: %s", self.path, e)
        if self.webhook:
            try:
                r = await self.http.post(self.webhook, json=discord_payload(report, self.mention, self.link(report)))
                r.raise_for_status()
                report["notified"] = True
            except httpx.HTTPError as e:
                log.warning("Discord webhook failed: %s", e)
                report["notified"] = False
        return report

    def recent(self, limit: int) -> list[dict]:
        return list(self.items)[-limit:][::-1]
