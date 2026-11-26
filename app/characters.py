"""Character cut-outs for the Nick 2009 "Up next", found by themselves.

fanart.tv keeps "character art" for TV shows: see-through PNGs of a show's characters, uploaded by fans
and checked by its moderators, with a count of likes. For each show (by its TheTVDB id, which Plex
gives Rerun), the best-liked English ones are fetched and checked before one is kept: a real see-through
background, big enough, smooth (anti-aliased) edges, the character not cut off at the top or sides and
filling a sensible share of the picture. It's saved as bugs/custom/characters/<Show>.png, where
images.character_for finds it. Cut-outs you put there yourself are never replaced.

fanart.tv needs a project API key (free: https://fanart.tv/get-an-api-key/, "Project API keys"), sent
as "api-key"; a personal API key can go with it as "client-key" (fanart.tv's terms: users may add their
own, sent in addition to the project's; it gets newer images sooner). Both are kept with Rerun's other
secrets ("fanart_key", "fanart_client"), never in the code.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time

import httpx
from PIL import Image

from . import images

log = logging.getLogger("rerun.characters")

API = "https://webservice.fanart.tv/v3/tv/{tvdb}"
TMDB_IDS = "https://api.themoviedb.org/3/tv/{tmdb}/external_ids"
DIR = images.CHARACTERS
MANIFEST = DIR / ".found.json"  # {show title: {"id", "file", "likes", "tried": [ids], "none": bool}}
MAX_BYTES = 8_000_000
TRY = 6  # downloads to check per show, at most
TEST_TVDB = "75886"  # SpongeBob SquarePants, for checking a key


def _read() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def _write(m: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=1))


def _foreign(art: dict) -> bool:
    """Character art in another language (English, or none at all, is what's wanted)."""
    return (art.get("lang") or "") not in ("en", "00", "")


def file_name(title: str) -> str:
    return (re.sub(r"[^\w .,'&()!-]+", "", title).strip()[:90] or "show") + ".png"


def judge(data: bytes) -> tuple[float, str]:
    """(score, "") for a cut-out good enough to use, or (0, why not)."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return 0, "not a picture"
    if im.mode not in ("RGBA", "LA") and not (im.mode == "P" and "transparency" in im.info):
        return 0, "no see-through background"
    im = im.convert("RGBA")
    w, h = im.size
    if min(w, h) < 400:
        return 0, f"too small ({w}x{h})"
    a = im.getchannel("A")
    hist = a.histogram()
    n = w * h
    clear = sum(hist[:16]) / n
    if not 0.12 <= clear <= 0.92:
        return 0, "the background isn't see-through" if clear < 0.12 else "almost nothing there"
    soft = sum(hist[16:240]) / max(1, n - sum(hist[:16]))
    if soft < 0.004:
        return 0, "jagged edges"
    box = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box or box[3] - box[1] < 300:
        return 0, "the character is too small"

    def solid(strip):  # share of a border strip that's solid
        hs = strip.histogram()
        return sum(hs[200:]) / max(1, sum(hs))
    if solid(a.crop((0, 0, w, 2))) > 0.05 or solid(a.crop((0, 0, 2, h))) > 0.15 or solid(a.crop((w - 2, 0, w, h))) > 0.15:
        return 0, "cut off at the edge"
    fill = (box[2] - box[0]) * (box[3] - box[1]) / n
    return 1 + min(w, h) / 1000 + fill + min(soft, 0.05) * 10, ""


class Grabber:
    def __init__(self, app):
        self.app = app
        self.progress = {"running": False, "done": 0, "total": 0, "found": 0, "step": "", "error": "", "finished": 0}
        self.task: asyncio.Task | None = None

    def key(self) -> str:
        return (self.app.secrets.data.get("fanart_key") or "").strip()

    def headers(self) -> dict:
        return _headers(self.key(), (self.app.secrets.data.get("fanart_client") or "").strip())

    # ---- what's there -----------------------------------------------------------------------
    def listing(self) -> list[dict]:
        m = _read()
        out = []
        for sid, show in sorted(self.app.catalog.shows.items(), key=lambda kv: kv[1].get("title", "").lower()):
            f = images.character_for(show.get("title", ""))
            rec = m.get(show.get("title", "")) or {}
            if f or rec.get("none"):
                out.append({"show": sid, "title": show["title"], "auto": bool(f and rec.get("file") and f.endswith(rec["file"])),
                            "file": f.split("/")[-1].split("\\")[-1] if f else "", "none": bool(rec.get("none") and not f)})
        return out

    # ---- fetching -----------------------------------------------------------------------------
    async def _tvdb(self, c: httpx.AsyncClient, show: dict) -> str:
        g = show.get("guids") or {}
        if g.get("tvdb"):
            return str(g["tvdb"])
        tk = (self.app.secrets.data.get("tmdb_key") or "").strip()
        if g.get("tmdb") and tk:
            try:
                r = await c.get(TMDB_IDS.format(tmdb=g["tmdb"]), params={"api_key": tk})
                if r.status_code == 200 and r.json().get("tvdb_id"):
                    return str(r.json()["tvdb_id"])
            except (httpx.HTTPError, ValueError):
                pass
        return ""

    async def candidates(self, c: httpx.AsyncClient, tvdb: str) -> list[dict]:
        r = await c.get(API.format(tvdb=tvdb), headers=self.headers())
        if r.status_code in (401, 403):
            raise PermissionError("fanart.tv didn't accept the API key")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        arts = r.json().get("characterart") or []

        return sorted(arts, key=lambda x: (_foreign(x), -int(x.get("likes") or 0)))

    async def for_show(self, c: httpx.AsyncClient, sid: str, skip: set | None = None) -> str:
        """Find and keep the best cut-out for one show. Its file name, or "" if none was good enough."""
        show = self.app.catalog.shows.get(sid) or {}
        title = show.get("title", "")
        m = _read()
        rec = m.get(title) or {}
        mine = images.character_for(title)
        if mine and not (rec.get("file") and mine.endswith(rec["file"])):
            return ""  # one you put there yourself
        tvdb = await self._tvdb(c, show)
        if not tvdb:
            return ""
        tried = set(rec.get("tried") or []) | (skip or set())
        best, best_score, best_data = None, 0.0, b""
        checked = 0
        for art in await self.candidates(c, tvdb):
            if art.get("id") in tried or checked >= TRY:
                continue
            if best and _foreign(art) and not _foreign(best):
                break  # a good English (or wordless) one beats any other language's, however liked
            checked += 1
            try:
                r = await c.get(art["url"])
                if r.status_code != 200 or len(r.content) > MAX_BYTES:
                    continue
            except httpx.HTTPError:
                continue
            score, why = await asyncio.to_thread(judge, r.content)
            if not score:
                log.info("Skipped a %s cut-out: %s", title, why)
                tried.add(art.get("id"))
                continue
            score += min(int(art.get("likes") or 0), 20) / 10
            if score > best_score:
                best, best_score, best_data = art, score, r.content
        m = _read()
        if not best:
            m[title] = {**(m.get(title) or {}), "tried": sorted(x for x in tried if x), "none": True}
            _write(m)
            return ""
        DIR.mkdir(parents=True, exist_ok=True)
        name = file_name(title)
        old = images.character_for(title)
        if old and not old.endswith(name):
            try:
                import os
                os.remove(old)
            except OSError:
                pass
        img = images._trim(Image.open(io.BytesIO(best_data)).convert("RGBA"))
        img.save(DIR / name)
        m[title] = {"id": best.get("id"), "file": name, "likes": int(best.get("likes") or 0),
                    "tried": sorted(x for x in tried | {best.get("id")} if x), "none": False}
        _write(m)
        return name

    async def another(self, sid: str) -> str:
        """A different cut-out for this show than the one it has (and any tried before)."""
        if not self.key():
            raise RuntimeError("Add a fanart.tv API key first")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Rerun"}) as c:
            return await self.for_show(c, sid)

    def remove(self, sid: str):
        """Take this show's found cut-out away, and don't look again (it goes without a picture)."""
        title = (self.app.catalog.shows.get(sid) or {}).get("title", "")
        m = _read()
        rec = m.get(title) or {}
        f = images.character_for(title)
        if f and rec.get("file") and f.endswith(rec["file"]):
            try:
                import os
                os.remove(f)
            except OSError:
                pass
        m[title] = {**rec, "file": "", "none": True}
        _write(m)

    # ---- all the shows ----------------------------------------------------------------------
    def start(self, only_new: bool = False) -> bool:
        if self.progress["running"]:
            return False
        if not self.key():
            raise RuntimeError("Add a fanart.tv API key first")
        self.progress.update(running=True, done=0, total=0, found=0, step="Starting", error="")
        self.task = asyncio.create_task(self._run(only_new))
        return True

    async def _run(self, only_new: bool):
        try:
            m = _read()
            todo = [sid for sid, s in self.app.catalog.shows.items()
                    if not images.character_for(s.get("title", "")) and not (m.get(s.get("title", "")) or {}).get("none")
                    and not (only_new and s.get("title", "") in m)]
            self.progress["total"] = len(todo)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Rerun"}) as c:
                for i, sid in enumerate(todo):
                    title = self.app.catalog.shows[sid].get("title", "")
                    self.progress.update(step=title, done=i)
                    try:
                        if await self.for_show(c, sid):
                            self.progress["found"] += 1
                    except PermissionError as e:
                        self.progress["error"] = str(e)
                        return
                    except (httpx.HTTPError, ValueError, OSError) as e:
                        log.info("Couldn't get a cut-out for %s: %s", title, e)
                    await asyncio.sleep(0.3)  # gently
            self.progress["done"] = self.progress["total"]
            log.info("Character cut-outs: found %s of %s", self.progress["found"], self.progress["total"])
        except Exception as e:
            log.exception("Getting character cut-outs failed")
            self.progress["error"] = f"{e.__class__.__name__}: {e}"
        finally:
            self.progress.update(running=False, step="", finished=time.time())


def _headers(key: str, client: str = "") -> dict:
    return {"api-key": key, **({"client-key": client} if client else {})}


async def test_key(key: str, client: str = "") -> str:
    """"" if fanart.tv accepts the project key (and the personal one, if given), else what's wrong."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Rerun"}) as c:
            r = await c.get(API.format(tvdb=TEST_TVDB), headers=_headers(key))
            if r.status_code in (401, 403):
                return "fanart.tv didn't accept that project key (a personal key goes in the second box)"
            if r.status_code != 200:
                return f"fanart.tv answered {r.status_code}"
            if client:
                r = await c.get(API.format(tvdb=TEST_TVDB), headers=_headers(key, client))
                if r.status_code in (401, 403):
                    return "fanart.tv didn't accept that personal key"
    except httpx.HTTPError as e:
        return f"Couldn't reach fanart.tv ({e.__class__.__name__})"
    return ""
