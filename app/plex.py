"""Talks to Plex: sign-in with a PIN, finding your server, and reading libraries."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from urllib.parse import quote, urlencode, urlsplit

import httpx

from . import __version__

log = logging.getLogger("rerun.plex")
PLEX_TV = "https://plex.tv"
PAGE = 500

TYPE_MOVIE, TYPE_SHOW, TYPE_EPISODE = 1, 2, 4


class PlexError(Exception):
    pass


def plex_headers(client_id: str, token: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": "Rerun",
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device-Name": "Rerun",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


async def create_pin(client_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=plex_headers(client_id))
        r.raise_for_status()
        d = r.json()
    url = "https://app.plex.tv/auth#?" + urlencode(
        {"clientID": client_id, "code": d["code"], "context[device][product]": "Rerun"}
    )
    return {"id": d["id"], "url": url}


async def check_pin(client_id: str, pin_id: int) -> str | None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", headers=plex_headers(client_id))
        r.raise_for_status()
        return r.json().get("authToken") or None


async def find_servers(client_id: str, token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{PLEX_TV}/api/v2/resources",
            params={"includeHttps": 1, "includeRelay": 0},
            headers=plex_headers(client_id, token),
        )
        r.raise_for_status()
        resources = r.json()
    servers = []
    for d in resources:
        if "server" not in (d.get("provides") or ""):
            continue
        servers.append({
            "name": d.get("name") or "Plex",
            "id": d.get("clientIdentifier"),
            "token": d.get("accessToken") or token,
            "owned": bool(d.get("owned")),
            "connections": [
                {"uri": x.get("uri"), "local": bool(x.get("local")), "address": x.get("address"),
                 "port": x.get("port"), "protocol": x.get("protocol")}
                for x in d.get("connections") or []
            ],
        })
    servers.sort(key=lambda s: not s["owned"])
    return servers


async def pick_connection(client_id: str, server: dict) -> str | None:
    """Try every way of reaching the server at once and keep the most direct one that works."""
    cands = ["http://127.0.0.1:32400"]
    conns = server.get("connections") or []
    cands += [f"http://{x['address']}:{x['port']}" for x in conns if x["local"] and x.get("address")]
    cands += [x["uri"] for x in conns if x["local"] and x.get("uri")]
    cands += [x["uri"] for x in conns if not x["local"] and x.get("uri")]
    cands = list(dict.fromkeys(cands))

    async def test(url):
        try:
            async with PlexClient(url, server["token"], client_id, timeout=5) as p:
                ident = await p.identity()
            return ident.get("machineIdentifier") == server["id"]
        except Exception:
            return False

    results = await asyncio.gather(*(test(u) for u in cands))
    for url, ok in zip(cands, results):
        if ok:
            return url
    return None


def _is_local_host(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class PlexClient:
    def __init__(self, url: str, token: str, client_id: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.url,
                timeout=self.timeout,
                headers=plex_headers(self.client_id, self.token),
                verify=not _is_local_host(self.url),
            )
        return self._http

    async def aclose(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def get(self, path: str, **params) -> dict:
        r = await self.http.get(path, params=params)
        if r.status_code == 401:
            raise PlexError("Plex rejected the sign-in. Reconnect Plex in Settings.")
        r.raise_for_status()
        return r.json().get("MediaContainer", {}) if r.content else {}

    async def identity(self) -> dict:
        return await self.get("/identity")

    async def sections(self) -> list[dict]:
        mc = await self.get("/library/sections")
        return [
            {"id": str(d["key"]), "title": d.get("title", ""), "type": d.get("type", "")}
            for d in mc.get("Directory") or []
        ]

    async def items(self, section_id: str, type_num: int):
        start = 0
        while True:
            mc = await self.get(
                f"/library/sections/{section_id}/all",
                type=type_num,
                includeGuids=1,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE},
            )
            batch = mc.get("Metadata") or []
            for m in batch:
                yield m
            start += len(batch)
            total = mc.get("totalSize")
            if not batch or (total is not None and start >= int(total)) or (total is None and len(batch) < PAGE):
                break

    async def collections(self, section_id: str) -> dict[str, list[str]]:
        """Map of item id -> names of the Plex collections it's in."""
        out: dict[str, list[str]] = {}
        try:
            mc = await self.get(f"/library/sections/{section_id}/collections")
        except Exception as e:
            log.info("Couldn't read collections for library %s (%s)", section_id, e.__class__.__name__)
            return out
        for col in mc.get("Metadata") or []:
            title = col.get("title") or ""
            try:
                kids = await self.get(f"/library/collections/{col['ratingKey']}/children")
            except Exception:
                continue
            for m in kids.get("Metadata") or []:
                out.setdefault("p" + str(m.get("ratingKey")), []).append(title)
        return out

    async def chapters(self, rating_key: str) -> list[float]:
        mc = await self.get(f"/library/metadata/{rating_key}", includeChapters=1)
        md = (mc.get("Metadata") or [{}])[0]
        return [c.get("startTimeOffset", 0) / 1000 for c in md.get("Chapter") or []]

    def part_url(self, part_key: str) -> str:
        return f"{self.url}{part_key}?X-Plex-Token={quote(self.token)}"

    async def image(self, path: str, width: int, height: int) -> bytes | None:
        r = await self.http.get(
            "/photo/:/transcode",
            params={"width": width, "height": height, "minSize": 1, "upscale": 1, "url": path},
        )
        return r.content if r.status_code == 200 else None


def pick_media(m: dict) -> dict | None:
    """Choose the best version of a Plex item (highest quality up to 1080p) and its first file."""
    medias = m.get("Media") or []
    if not medias:
        return None

    def score(x):
        h = int(x.get("height") or 0)
        return (h > 1080, -h if h <= 1080 else h)

    best = sorted(medias, key=score)[0]
    parts = best.get("Part") or []
    if not parts:
        return None
    p = parts[0]
    dur = (p.get("duration") or best.get("duration") or m.get("duration") or 0) / 1000
    analysed = "videoCodec" in best or "audioCodec" in best
    return {
        "file": p.get("file") or "",
        "part": p.get("key") or "",
        "dur": round(dur, 3),
        "audio": bool(best.get("audioCodec")) if analysed else True,
    }


def parse_guids(m: dict) -> dict:
    out = {}
    for g in m.get("Guid") or []:
        gid = g.get("id", "")
        if "://" in gid:
            kind, val = gid.split("://", 1)
            out[kind] = val
    legacy = m.get("guid", "")
    if "thetvdb://" in legacy:
        out.setdefault("tvdb", legacy.split("thetvdb://", 1)[1].split("?")[0].split("/")[0])
    elif "themoviedb://" in legacy:
        out.setdefault("tmdb", legacy.split("themoviedb://", 1)[1].split("?")[0].split("/")[0])
    elif "imdb://" in legacy:
        out.setdefault("imdb", legacy.split("imdb://", 1)[1].split("?")[0])
    return out


def tags(m: dict, key: str) -> list[str]:
    return [t["tag"] for t in m.get(key) or [] if t.get("tag")]
