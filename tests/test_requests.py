import asyncio
from types import SimpleNamespace

from app.requests import Seerr, _item, request_status


def req(status, media_status=None, download=None):
    return {"status": status, "media": {"status": media_status, "downloadStatus": download or []}}


def test_request_status_follows_the_library_first():
    assert request_status(req(2, 5)) == "available"
    assert request_status(req(2, 4)) == "partial"
    assert request_status(req(1, 2)) == "pending"
    assert request_status(req(2, 3)) == "approved"
    assert request_status(req(2, 3, [{"size": 100, "sizeLeft": 40}])) == "downloading"
    assert request_status(req(3, 2)) == "declined"
    assert request_status(req(4, 3)) == "failed"


def test_item_flattens_search_results():
    item = _item({"id": 603, "mediaType": "movie", "title": "The Matrix", "releaseDate": "1999-03-31",
                  "posterPath": "/p.jpg", "mediaInfo": {"status": 5, "jellyfinMediaId": "abc",
                                                         "downloadStatus": [{"size": 10, "sizeLeft": 5}]}})
    assert item["type"] == "movie" and item["tmdbId"] == 603 and item["year"] == 1999
    assert item["poster"].endswith("/p.jpg")
    assert item["status"] == "available" and item["jellyfinId"] == "abc"
    assert item["download"]["progress"] == 0.5


def test_unknown_jellyfin_users_are_imported_once():
    seerr = Seerr(SimpleNamespace(seerr_url="http://seerr", seerr_api_key="k"))
    users = [{"id": 1, "jellyfinUserId": "aaaa"}]
    calls = []

    async def call(method, path, user=None, **kwargs):
        calls.append((method, path))
        if path == "/user":
            return {"results": users}
        if path == "/user/import-from-jellyfin":
            users.append({"id": 7, "jellyfinUserId": kwargs["json"]["jellyfinUserIds"][0]})
            return []

    seerr._call = call
    assert asyncio.run(seerr.user_id("AAAA")) == 1
    assert asyncio.run(seerr.user_id("abcd-ef12")) == 7
    assert asyncio.run(seerr.user_id("abcdef12")) == 7
    assert calls.count(("POST", "/user/import-from-jellyfin")) == 1
