"""Find which TV network a show originally aired on.

Order of preference: TMDB (if you gave a key) -> what Plex already knows ->
TVMaze (free, no key). Answers are cached on disk so rescans are instant and
the services are only asked once per show. Only show IDs are ever sent.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.metadata")

TMDB = "https://api.themoviedb.org/3"
TVMAZE = "https://api.tvmaze.com"
HIT_DAYS = 180
MISS_DAYS = 14


class NetworkLookup:
    def __init__(self, tmdb_key: str, online: bool):
        self.path = DATA_DIR / "metadata_cache.json"
        self.cache: dict = read_json(self.path, {})
        self.tmdb_key = (tmdb_key or "").strip()
        self.online = online
        self.tmdb_ok = bool(self.tmdb_key)
        self.http: httpx.AsyncClient | None = None
        self._maze_lock = asyncio.Lock()
        self._maze_last = 0.0

    async def __aenter__(self):
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Rerun"})
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        self.cache = {k: v for k, v in self.cache.items() if v.get("x", 0) > time.time()}
        atomic_write_json(self.path, self.cache)

    def _get(self, key):
        hit = self.cache.get(key)
        if hit and hit.get("x", 0) > time.time():
            return hit
        return None

    def _put(self, key, value):
        days = HIT_DAYS if value else MISS_DAYS
        self.cache[key] = {"v": value, "x": time.time() + days * 86400}
        return value

    async def show_network(self, guids: dict, studio: str) -> tuple[str, str]:
        """Return (network name, TMDB logo path)."""
        if self.online and self.tmdb_ok:
            tid = guids.get("tmdb") or await self._tmdb_find(guids)
            if tid:
                info = await self._tmdb_tv(tid)
                if info and info.get("network"):
                    return info["network"], info.get("logo") or ""
        if studio:
            return studio, ""
        if self.online:
            net = await self._tvmaze(guids)
            if net:
                return net, ""
        return "", ""

    async def network_logo(self, tmdb_tv_id: str) -> str:
        info = await self._tmdb_tv(tmdb_tv_id) if self.tmdb_ok else None
        return (info or {}).get("logo") or ""

    async def _tmdb(self, path: str, params: dict | None = None):
        params = dict(params or {})
        headers = {"Accept": "application/json"}
        if self.tmdb_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.tmdb_key}"
        else:
            params["api_key"] = self.tmdb_key
        for attempt in range(3):
            try:
                r = await self.http.get(f"{TMDB}{path}", params=params, headers=headers)
            except httpx.HTTPError as e:
                log.warning("TMDB unreachable (%s)", e.__class__.__name__)
                return None
            if r.status_code == 429:
                await asyncio.sleep(2 + attempt * 2)
                continue
            if r.status_code == 401:
                if self.tmdb_ok:
                    log.warning("TMDB rejected the key; skipping TMDB lookups")
                self.tmdb_ok = False
                return None
            if r.status_code == 404:
                return {}
            if r.status_code >= 400:
                return None
            return r.json()
        return None

    async def _tmdb_tv(self, tid: str):
        key = f"tmdb:tv:{tid}"
        hit = self._get(key)
        if hit:
            return hit["v"]
        d = await self._tmdb(f"/tv/{tid}")
        if d is None:
            return None  # transient failure; don't cache
        nets = d.get("networks") or []
        value = {"network": nets[0]["name"], "logo": nets[0].get("logo_path") or ""} if nets else {}
        return self._put(key, value)

    async def _tmdb_find(self, guids: dict) -> str:
        for kind, source in (("tvdb", "tvdb_id"), ("imdb", "imdb_id")):
            ext = guids.get(kind)
            if not ext:
                continue
            key = f"tmdb:find:{kind}:{ext}"
            hit = self._get(key)
            if hit:
                if hit["v"]:
                    return hit["v"]
                continue
            d = await self._tmdb(f"/find/{ext}", {"external_source": source})
            if d is None:
                continue
            res = d.get("tv_results") or []
            tid = str(res[0]["id"]) if res else ""
            self._put(key, tid)
            if tid:
                return tid
        return ""

    async def _tvmaze(self, guids: dict) -> str:
        for kind, param in (("tvdb", "thetvdb"), ("imdb", "imdb")):
            ext = guids.get(kind)
            if not ext:
                continue
            key = f"tvmaze:{kind}:{ext}"
            hit = self._get(key)
            if hit:
                if hit["v"]:
                    return hit["v"]
                continue
            d = await self._maze_get(f"/lookup/shows", {param: ext})
            if d is None:
                continue
            net = ((d.get("network") or {}).get("name") or (d.get("webChannel") or {}).get("name") or "")
            self._put(key, net)
            if net:
                return net
        return ""

    async def _maze_get(self, path: str, params: dict):
        # TVMaze allows ~20 requests per 10 seconds; stay well under it.
        async with self._maze_lock:
            wait = self._maze_last + 0.6 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._maze_last = time.monotonic()
            for attempt in range(3):
                try:
                    r = await self.http.get(f"{TVMAZE}{path}", params=params)
                except httpx.HTTPError:
                    return None
                if r.status_code == 429:
                    await asyncio.sleep(5 + attempt * 5)
                    continue
                if r.status_code == 404:
                    return {}
                if r.status_code >= 400:
                    return None
                return r.json()
        return None


async def test_tmdb_key(key: str) -> bool:
    lookup = NetworkLookup(key, True)
    async with httpx.AsyncClient(timeout=10) as http:
        lookup.http = http
        d = await lookup._tmdb("/configuration")
    return bool(d) and lookup.tmdb_ok
