"""Dispatcharr client. Logs in once and refreshes the JWT; Dispatcharr throttles repeated logins. Reads channels, groups,
logos and the guide; the only write is a channel's stream list (backup streams, when enabled)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

log = logging.getLogger("clipstream.dispatcharr")


class Dispatcharr:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self._credentials = {"username": username, "password": password}
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60)
        self._access: str | None = None
        self._refresh: str | None = None
        self._lock = asyncio.Lock()

    async def close(self):
        await self._client.aclose()

    async def _login(self):
        for attempt in range(6):
            r = await self._client.post("/api/accounts/token/", json=self._credentials)
            if r.status_code == 429:  # throttled: wait it out
                await asyncio.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            tokens = r.json()
            self._access, self._refresh = tokens["access"], tokens.get("refresh")
            return
        raise RuntimeError("Dispatcharr login kept being throttled")

    async def _renew(self):
        async with self._lock:
            if self._refresh:
                r = await self._client.post("/api/accounts/token/refresh/", json={"refresh": self._refresh})
                if r.status_code == 200:
                    self._access = r.json()["access"]
                    return
            await self._login()

    async def request(self, method: str, path: str, json=None, **params):
        if self._access is None:
            await self._renew()
        for _ in range(2):
            r = await self._client.request(method, path, params=params or None, json=json,
                                           headers={"Authorization": f"Bearer {self._access}"})
            if r.status_code == 401:
                await self._renew()
                continue
            r.raise_for_status()
            return r.json() if r.content else None
        raise RuntimeError(f"Dispatcharr refused {path}")

    async def get(self, path: str, **params):
        return await self.request("GET", path, **params)

    @staticmethod
    def _results(data):
        return data.get("results", data) if isinstance(data, dict) else data

    async def channels(self) -> list[dict]:
        # Hidden channels too (the list leaves them out by default): channel upkeep needs the twins it hid.
        # Views drop them via hidden_from_output.
        return self._results(await self.get("/api/channels/channels/", page_size=10000, visibility_filter="all"))

    async def groups(self) -> dict[int, str]:
        return {g["id"]: g["name"] for g in self._results(await self.get("/api/channels/groups/"))}

    async def logos(self) -> dict[int, dict]:
        return {l["id"]: l for l in self._results(await self.get("/api/channels/logos/", page_size=10000))}

    async def guide(self, start: datetime, end: datetime) -> list[dict]:
        data = await self.get("/api/epg/grid/", start=start.isoformat(), end=end.isoformat())
        return data.get("data", []) if isinstance(data, dict) else data

    async def m3u_accounts(self) -> list[dict]:
        return self._results(await self.get("/api/m3u/accounts/"))

    async def epg_sources(self) -> list[dict]:
        return self._results(await self.get("/api/epg/sources/"))

    async def epg_entries(self) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 50):
            data = await self.get("/api/epg/epgdata/", page_size=5000, page=page)
            out += self._results(data)
            if not (isinstance(data, dict) and data.get("next")):
                break
        return out

    async def streams_by_id(self, ids) -> dict[int, dict]:
        ids, out = sorted(set(ids)), {}
        for i in range(0, len(ids), 500):
            data = await self.request("POST", "/api/channels/streams/by-ids/", json={"ids": ids[i:i + 500]}, page_size=1000)
            out.update({s["id"]: s for s in self._results(data)})
        return out

    async def streams(self, max_pages: int = 10, **filters) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self.get("/api/channels/streams/", page_size=2000, page=page, **filters)
            out += self._results(data)
            if not (isinstance(data, dict) and data.get("next")):
                break
        return out

    async def set_streams(self, channel_id: int, stream_ids: list[int]):
        await self.update_channel(channel_id, {"streams": stream_ids})

    async def update_channel(self, channel_id: int, fields: dict):
        await self.request("PATCH", f"/api/channels/channels/{channel_id}/", json=fields)

    async def edit_channels(self, edits: list[dict]):
        for i in range(0, len(edits), 200):
            await self.request("PATCH", "/api/channels/channels/edit/bulk/", json=edits[i:i + 200])

    def stream_url(self, public_base: str, channel_uuid: str) -> str:
        return f"{public_base}/proxy/ts/stream/{channel_uuid}"
