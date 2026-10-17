from app.upgrades import only_upgrades


def rec(event, title, date, reason=None):
    return {"eventType": event, "movie": {"tmdbId": title}, "date": date, "data": {"reason": reason} if reason else {}}


KEY = lambda r: r["movie"]["tmdbId"]


def test_an_import_with_an_upgrade_deletion_is_an_upgrade():
    records = [rec("downloadFolderImported", 1, "2026-10-09T05:34:10Z"),
               rec("movieFileDeleted", 1, "2026-10-09T05:34:08Z", "Upgrade"),
               rec("downloadFolderImported", 2, "2026-10-09T02:00:00Z")]
    assert only_upgrades(records, KEY, "movieFileDeleted") == {1}


def test_a_new_title_upgraded_later_is_still_new():
    records = [rec("downloadFolderImported", 3, "2026-10-01T10:00:00Z"),
               rec("downloadFolderImported", 3, "2026-10-05T10:00:00Z"),
               rec("movieFileDeleted", 3, "2026-10-05T10:00:01Z", "Upgrade")]
    assert only_upgrades(records, KEY, "movieFileDeleted") == set()


def test_other_deletions_dont_count():
    records = [rec("downloadFolderImported", 4, "2026-10-05T10:00:00Z"),
               rec("movieFileDeleted", 4, "2026-10-05T10:00:01Z", "MissingFromDisk")]
    assert only_upgrades(records, KEY, "movieFileDeleted") == set()


def test_callers_waiting_together_share_one_fetch():
    import asyncio
    from types import SimpleNamespace
    from app.upgrades import Upgrades

    up = Upgrades(SimpleNamespace(radarr_url="r", radarr_api_key="k", sonarr_url=None, sonarr_api_key=None))
    calls = []

    async def history(base, key, params):
        calls.append(base)
        await asyncio.sleep(0.05)
        return [rec("downloadFolderImported", 1, "2026-10-09T05:34:10Z"),
                rec("movieFileDeleted", 1, "2026-10-09T05:34:08Z", "Upgrade")]

    up._history = history

    async def run():
        return await asyncio.gather(up.get(), up.get(), up.get())

    results = asyncio.run(run())
    assert calls == ["r"]
    assert all(r["movies"]["tmdb"] == [1] for r in results)
