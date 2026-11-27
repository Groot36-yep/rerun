"""Character cut-outs for the Nick 2009 "Up next", found by themselves.

fanart.tv keeps "character art" for TV shows: see-through PNGs of a show's characters, uploaded by fans
and checked by its moderators, with a count of likes. For each show (by its TheTVDB id, which Plex
gives Rerun; failing that, found from its IMDb or TMDB id through TVMaze or Wikidata, which need no
key), the best-liked English ones are fetched and checked before one is kept: a real see-through
background, big enough, smooth (anti-aliased) edges, the character not cut off at the top or sides and
filling a sensible share of the picture. If none passes, the show's "HD ClearArt" (its characters with
its logo, also see-through) is tried the same way. It's saved as bugs/custom/characters/<Show>.png,
where images.character_for finds it. Cut-outs you put there yourself are never replaced.

(Fan wikis were tried too, but their pictures sit behind a bot check, and getting past that would mean
pretending to be a browser, so they're not used.)

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
TVMAZE = "https://api.tvmaze.com/lookup/shows"
WIKIDATA = "https://www.wikidata.org/w/api.php"
WIKIDATA_ITEM = "https://www.wikidata.org/wiki/Special:EntityData/{item}.json"
KINDS = (("characterart", "character"), ("hdclearart", "clear art"))  # in order of preference
SEARCH_V = 2  # shows that found nothing with an older search are searched again
DIR = images.CHARACTERS
MANIFEST = DIR / ".found.json"  # {show title: {"id", "file", "kind", "likes", "tried": [ids], "none": bool,
#                                  "removed": bool (you took it away), "why": str, "v": SEARCH_V}}
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
            # every show: one without a cut-out is listed too, so you can add your own
            out.append({"show": sid, "title": show["title"], "auto": bool(f and rec.get("file") and f.endswith(rec["file"])),
                        "file": f.split("/")[-1].split("\\")[-1] if f else "", "none": bool(rec.get("none") and not f),
                        "kind": rec.get("kind", "character") if f else "",
                        "own": bool(f and rec.get("own")),
                        "why": "" if f else ("You removed it" if rec.get("removed") else rec.get("why")
                                             or ("Not looked for yet" if not rec else ""))})
        return out

    # ---- fetching -----------------------------------------------------------------------------
    async def _tvdb(self, c: httpx.AsyncClient, show: dict) -> str:
        """The show's TheTVDB id: from Plex, else from its TMDB id (with a TMDB key), else looked up from
        its IMDb id on TVMaze, else from its TMDB or IMDb id on Wikidata."""
        g = show.get("guids") or {}
        if g.get("tvdb"):
            return str(g["tvdb"])
        tk = (self.app.secrets.data.get("tmdb_key") or "").strip()
        try:
            if g.get("tmdb") and tk:
                r = await c.get(TMDB_IDS.format(tmdb=g["tmdb"]), params={"api_key": tk})
                if r.status_code == 200 and r.json().get("tvdb_id"):
                    return str(r.json()["tvdb_id"])
            if g.get("imdb"):
                r = await c.get(TVMAZE, params={"imdb": g["imdb"]})
                if r.status_code == 200 and ((r.json() or {}).get("externals") or {}).get("thetvdb"):
                    return str(r.json()["externals"]["thetvdb"])
            for prop, val in (("P4983", g.get("tmdb")), ("P345", g.get("imdb"))):
                if not val:
                    continue
                r = await c.get(WIKIDATA, params={"action": "query", "list": "search", "format": "json",
                                                  "srsearch": f"haswbstatement:{prop}={val}"}, headers={"User-Agent": images.WIKI_UA})
                hits = ((r.json().get("query") or {}).get("search") or []) if r.status_code == 200 else []
                for hit in hits[:2]:
                    e = await c.get(WIKIDATA_ITEM.format(item=hit["title"]), headers={"User-Agent": images.WIKI_UA})
                    claims = ((e.json().get("entities") or {}).get(hit["title"]) or {}).get("claims") or {} if e.status_code == 200 else {}
                    for cl in claims.get("P4835") or []:
                        v = ((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                        if v:
                            return str(v)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        return ""

    async def candidates(self, c: httpx.AsyncClient, tvdb: str) -> dict:
        """{"characterart": [...], "hdclearart": [...]}, each best first (English, then most liked)."""
        r = await c.get(API.format(tvdb=tvdb), headers=self.headers())
        if r.status_code in (401, 403):
            raise PermissionError("fanart.tv didn't accept the API key")
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        j = r.json()
        return {k: sorted(j.get(k) or [], key=lambda x: (_foreign(x), -int(x.get("likes") or 0))) for k, _ in KINDS}

    async def _pick(self, c, title: str, arts: list, tried: set, why: list):
        """The best of these that passes the checks: (art, data) or (None, b"")."""
        best, best_score, best_data = None, 0.0, b""
        checked = 0
        for art in arts:
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
            score, bad = await asyncio.to_thread(judge, r.content)
            if not score:
                log.info("Skipped a %s picture: %s", title, bad)
                why.append(bad)
                tried.add(art.get("id"))
                continue
            score += min(int(art.get("likes") or 0), 20) / 10
            if score > best_score:
                best, best_score, best_data = art, score, r.content
        return best, best_data

    async def for_show(self, c: httpx.AsyncClient, sid: str, skip: set | None = None) -> str:
        """Find and keep the best cut-out for one show. Its file name, or "" if none was good enough."""
        show = self.app.catalog.shows.get(sid) or {}
        title = show.get("title", "")
        m = _read()
        rec = m.get(title) or {}
        mine = images.character_for(title)
        if mine and not (rec.get("file") and mine.endswith(rec["file"])):
            return ""  # one you put there yourself
        tried = set(rec.get("tried") or []) | (skip or set())

        def nothing(why: str) -> str:
            m = _read()
            m[title] = {**(m.get(title) or {}), "tried": sorted(x for x in tried if x), "none": True,
                        "removed": False, "why": why, "v": SEARCH_V, "file": ""}
            _write(m)
            return ""

        tvdb = await self._tvdb(c, show)
        if not tvdb:
            return nothing("Couldn't identify the show (Plex gave no TheTVDB, IMDb or TMDB id that matched)")
        arts = await self.candidates(c, tvdb)
        if not any(arts.get(k) for k, _ in KINDS):
            return nothing("fanart.tv has no character art for it")
        why: list = []
        best, best_data, kind = None, b"", ""
        for k, label in KINDS:
            best, best_data = await self._pick(c, title, arts.get(k) or [], tried, why)
            if best:
                kind = label
                break
        if not best:
            if not why:
                return nothing("Nothing new on fanart.tv to try")
            common = sorted(set(why), key=why.count, reverse=True)[:2]
            return nothing(f"fanart.tv's pictures weren't good enough ({', '.join(common)})")
        m = _read()
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
        m[title] = {"id": best.get("id"), "file": name, "kind": kind, "likes": int(best.get("likes") or 0),
                    "tried": sorted(x for x in tried | {best.get("id")} if x), "none": False, "v": SEARCH_V}
        _write(m)
        return name

    async def another(self, sid: str) -> str:
        """A different cut-out for this show than the one it has (and any tried before)."""
        if not self.key():
            raise RuntimeError("Add a fanart.tv API key first")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Rerun"}) as c:
            return await self.for_show(c, sid)

    def upload(self, sid: str, data: bytes) -> str:
        """Your own cut-out for a show, dropped onto the page: it must be a picture with a see-through
        background (else it would sit on the bar as a box). Trimmed of empty edges and kept as the show's
        cut-out, in place of any found one; never replaced by the finder."""
        title = (self.app.catalog.shows.get(sid) or {}).get("title", "")
        if not title:
            raise ValueError("No such show")
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:
            raise ValueError("That isn't a picture Rerun can read. Use a PNG.")
        if im.mode not in ("RGBA", "LA") and not (im.mode == "P" and "transparency" in im.info):
            raise ValueError("That picture has no see-through background, so it would show as a box. "
                             "Use a PNG cut-out of the character.")
        im = images._trim(im.convert("RGBA"))
        if im.getchannel("A").getextrema()[0] > 200:
            raise ValueError("That picture has no see-through background, so it would show as a box. "
                             "Use a PNG cut-out of the character.")
        if max(im.size) < 120:
            raise ValueError("That picture is too small to look good on the screen.")
        DIR.mkdir(parents=True, exist_ok=True)
        old = images.character_for(title)
        name = file_name(title)
        if old and not old.endswith(name):
            try:
                import os
                os.remove(old)
            except OSError:
                pass
        im.save(DIR / name)
        m = _read()
        m[title] = {"own": True, "none": False, "removed": False, "v": SEARCH_V}  # yours: never replaced
        _write(m)
        return name

    def remove(self, sid: str):
        """Take this show's found cut-out away, and don't look again (it goes without a picture)."""
        title = (self.app.catalog.shows.get(sid) or {}).get("title", "")
        m = _read()
        rec = m.get(title) or {}
        f = images.character_for(title)
        if f and ((rec.get("file") and f.endswith(rec["file"])) or rec.get("own")):  # found, or added on the page
            try:
                import os
                os.remove(f)
            except OSError:
                pass
        m[title] = {**rec, "file": "", "own": False, "none": True, "removed": True}
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
            def wanted(s: dict) -> bool:
                rec = m.get(s.get("title", ""))
                if images.character_for(s.get("title", "")) or (rec or {}).get("removed"):
                    return False
                if rec is None:
                    return True
                # found nothing before: look again with newer sources, but not every night
                return not only_new and rec.get("none") and int(rec.get("v") or 1) < SEARCH_V
            todo = [sid for sid, s in self.app.catalog.shows.items() if wanted(s)]
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
