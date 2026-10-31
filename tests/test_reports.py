import asyncio
import json

import httpx

from app.reports import Reports, discord_payload, file_facts, item_title

EPISODE = {"Type": "Episode", "SeriesName": "Dexter: Resurrection", "ParentIndexNumber": 2, "IndexNumber": 1,
           "Name": "Pilot", "MediaSources": [{"Container": "mkv", "Size": 3 * 1024 ** 3, "MediaStreams": [
               {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "VideoRangeType": "DOVI"},
               {"Type": "Audio", "Language": "eng"}, {"Type": "Audio", "Language": "spa"},
               {"Type": "Subtitle", "Language": "eng"}]}]}


def test_titles():
    assert item_title(EPISODE) == "Dexter: Resurrection S2E1 · Pilot"
    assert item_title({"Type": "Movie", "Name": "Logan", "ProductionYear": 2017}) == "Logan (2017)"


def test_file_facts():
    assert file_facts(EPISODE) == "mkv · 3.00 GB · hevc 3840x2160 DOVI · 2 audio (eng, spa) · 1 subtitles"
    assert "no video stream" in file_facts({"MediaSources": [{"Container": "mp4", "MediaStreams": []}]})
    assert file_facts({"MediaSources": []}) == "Jellyfin lists no media source for this item"


def test_build_and_payload():
    r = Reports(None, None, "https://watch.example.com", None)
    rep = r.build({"itemId": "abc", "kind": "audio", "message": "  out of sync after 20 min ", "positionSeconds": 4000.5,
                   "app": "Android TV 0.4.1"}, {"id": "u1", "name": "jcarr"}, EPISODE)
    assert rep["position"] == "1:06:40" and rep["message"] == "out of sync after 20 min"
    p = discord_payload(rep, "<@42>", r.link(rep))
    assert p["content"] == "<@42>"
    e = p["embeds"][0]
    assert e["title"] == "Problem report: Dexter: Resurrection S2E1 · Pilot"
    assert e["url"] == "https://watch.example.com/web/#/details?id=abc"
    assert {"name": "Problem", "value": "Sound problem (missing, out of sync, wrong language)", "inline": True} in e["fields"]
    assert r.build({"kind": "nonsense"}, {"name": "x"}, None)["kind"] == "other"


def test_rate_limit():
    r = Reports(None, None, None, None)
    r.sent["u1"] = [1000.0] * 10
    assert not r.allowed("u1", now=1500.0)
    assert r.allowed("u1", now=1000.0 + 3601)


def test_posts_to_the_webhook_and_keeps_a_file(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    path = tmp_path / "reports.jsonl"
    r = Reports("https://discord.example/webhook", None, None, str(path))
    r.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rep = asyncio.run(r.add(r.build({"message": "won't start"}, {"id": "u1", "name": "jcarr"}, None)))
    assert rep["notified"] and seen[0]["embeds"][0]["description"] == "won't start"
    assert Reports(None, None, None, str(path)).recent(5)[0]["id"] == rep["id"]


def test_webhook_url_forgives_pasting():
    from app.reports import webhook_url
    full = "https://discord.com/api/webhooks/123/abc"
    assert webhook_url("​" + full) == full
    assert webhook_url("﻿https://discord.com/api/webhooks/123/abc") == full
    assert webhook_url(full) == full
    assert webhook_url(f'"{full}"') == full
    assert webhook_url(" discord.com/api/webhooks/123/abc ") == full
    assert webhook_url("1565930024287342632/" + "x" * 30) == "https://discord.com/api/webhooks/1565930024287342632/" + "x" * 30
    assert webhook_url("abc") is None
    assert webhook_url("") is None


def test_mention_forms():
    from app.reports import mention_text
    for v in ("575698426986233870", "@575698426986233870", "<@575698426986233870>", " <@!575698426986233870> "):
        assert mention_text(v) == "<@575698426986233870>"
    assert mention_text("<@&123456789>") == "<@&123456789>"
    assert mention_text("") is None
