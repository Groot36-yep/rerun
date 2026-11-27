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
import os
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
SEARCH_V = 3  # shows with fewer than WANT found with an older search are searched again (3: several a show)
WANT = 3       # cut-outs to find for each show, for variety on the air
MAX_PICS = 6   # a show's cut-outs in all, found and your own
DIR = images.CHARACTERS
MANIFEST = DIR / ".found.json"  # {show title: {"pics": [{"file", "id", "kind", "own"}], "tried": [ids],
#                                  "none": bool, "removed": bool (you took them all away), "why": str, "v": SEARCH_V}}
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


def _rec(m: dict, title: str) -> dict:
    """A show's record, with its pictures as a list (older records had one "file" or "own")."""
    rec = dict(m.get(title) or {})
    if "pics" not in rec:
        pics = []
        if rec.get("file"):
            pics.append({"file": rec["file"], "id": rec.get("id"), "kind": rec.get("kind", "character"), "own": False})
        elif rec.get("own"):
            pics.append({"file": file_name(title), "id": None, "kind": "character", "own": True})
        rec["pics"] = pics
    rec["pics"] = [x for x in rec["pics"] if (DIR / x["file"]).is_file()]
    return rec


def _next_name(title: str) -> str:
    """The file for a show's next picture: "<Show>.png", then "<Show> #2.png", "#3"..."""
    first = file_name(title)
    if not (DIR / first).exists():
        return first
    base = first[:-4]
    n = 2
    while (DIR / f"{base} #{n}.png").exists():
        n += 1
    return f"{base} #{n}.png"


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
    def _pics(self, title: str, rec: dict) -> list[dict]:
        """A show's pictures: found ones, ones added on the page, and any put in the folder by hand."""
        known = {x["file"]: x for x in rec["pics"]}
        out = []
        for f in images.characters_for(title):
            name = os.path.basename(f)
            out.append(known.get(name) or {"file": name, "id": None, "kind": "character", "own": True})
        return out

    def listing(self) -> list[dict]:
        m = _read()
        out = []
        for sid, show in sorted(self.app.catalog.shows.items(), key=lambda kv: kv[1].get("title", "").lower()):
            title = show.get("title", "")
            rec = _rec(m, title)
            pics = self._pics(title, rec)
            # every show: one without a cut-out is listed too, so you can add your own
            out.append({"show": sid, "title": title,
                        "pics": [{"file": x["file"], "own": bool(x.get("own")), "kind": x.get("kind", "character")}
                                 for x in pics],
                        "file": pics[0]["file"] if pics else "",
                        "why": "" if pics else ("You removed them" if rec.get("removed") else rec.get("why")
                                                or ("Not looked for yet" if title not in m else ""))})
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

    async def _pick(self, c, title: str, arts: list, tried: set, why: list, need: int) -> list:
        """Up to `need` of these that pass the checks, best first: [(art, data)]. English (or wordless)
        ones come before other languages', however liked."""
        good = []
        checked = 0
        for art in arts:
            if art.get("id") in tried or checked >= TRY + 2 * need:
                continue
            if len(good) >= need and _foreign(art):
                break
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
            good.append((score + min(int(art.get("likes") or 0), 20) / 10 - (2 if _foreign(art) else 0), art, r.content))
        good.sort(key=lambda g: -g[0])
        return [(art, data) for _s, art, data in good[:need]]

    async def for_show(self, c: httpx.AsyncClient, sid: str, more: bool = False) -> list[str]:
        """Find good cut-outs for one show, up to WANT of them found (or, with `more`, one more than it has,
        up to MAX_PICS in all). The new files' names; [] if none was good enough."""
        show = self.app.catalog.shows.get(sid) or {}
        title = show.get("title", "")
        m = _read()
        rec = _rec(m, title)
        pics = self._pics(title, rec)
        found = [x for x in pics if not x.get("own")]
        need = 1 if more else WANT - len(found)
        need = min(need, MAX_PICS - len(pics))
        if need <= 0:
            return []
        tried = set(rec.get("tried") or []) | {x["id"] for x in pics if x.get("id")}

        def nothing(why: str) -> list:
            m = _read()
            r = _rec(m, title)
            m[title] = {**r, "tried": sorted(x for x in tried if x), "none": not r["pics"], "removed": False,
                        "why": why, "v": SEARCH_V}
            _write(m)
            return []

        tvdb = await self._tvdb(c, show)
        if not tvdb:
            return nothing("Couldn't identify the show (Plex gave no TheTVDB, IMDb or TMDB id that matched)")
        arts = await self.candidates(c, tvdb)
        if not any(arts.get(k) for k, _ in KINDS):
            return nothing("fanart.tv has no character art for it")
        why: list = []
        picks: list = []
        for k, label in KINDS:  # character art first; clear art (characters with the logo) if that's short
            if len(picks) < need:
                picks += [(art, data, label) for art, data in
                          await self._pick(c, title, arts.get(k) or [], tried, why, need - len(picks))]
        if not picks:
            if pics:
                return nothing("No more good ones on fanart.tv")
            if not why:
                return nothing("Nothing new on fanart.tv to try")
            common = sorted(set(why), key=why.count, reverse=True)[:2]
            return nothing(f"fanart.tv's pictures weren't good enough ({', '.join(common)})")
        DIR.mkdir(parents=True, exist_ok=True)
        names = []
        new = []
        for art, data, kind in picks:
            name = _next_name(title)
            images._trim(Image.open(io.BytesIO(data)).convert("RGBA")).save(DIR / name)
            names.append(name)
            new.append({"file": name, "id": art.get("id"), "kind": kind, "own": False})
            tried.add(art.get("id"))
        m = _read()
        r = _rec(m, title)
        m[title] = {**r, "pics": r["pics"] + new, "tried": sorted(x for x in tried if x), "none": False,
                    "removed": False, "why": "", "v": SEARCH_V}
        _write(m)
        return names

    async def another(self, sid: str) -> list[str]:
        """One more cut-out for this show, different from the ones it has (and any tried before)."""
        if not self.key():
            raise RuntimeError("Add a fanart.tv API key first")
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Rerun"}) as c:
            return await self.for_show(c, sid, more=True)

    def upload(self, sid: str, data: bytes) -> str:
        """Your own cut-out for a show, dropped onto the page, added to its others: it must be a picture with
        a see-through background (else it would sit on the bar as a box). Trimmed of empty edges, and never
        replaced by the finder."""
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
        m = _read()
        rec = _rec(m, title)
        if len(self._pics(title, rec)) >= MAX_PICS:
            raise ValueError(f"{title} already has {MAX_PICS} pictures. Remove one first.")
        DIR.mkdir(parents=True, exist_ok=True)
        name = _next_name(title)
        im.save(DIR / name)
        m[title] = {**rec, "pics": rec["pics"] + [{"file": name, "id": None, "kind": "character", "own": True}],
                    "none": False, "removed": False, "why": "", "v": rec.get("v") or SEARCH_V}
        _write(m)
        return name

    def remove(self, sid: str, file: str = ""):
        """Take one of a show's pictures away (or, with no `file`, all of them). A found one isn't
        fetched again; a show left with none isn't looked for again (it goes without a picture)."""
        title = (self.app.catalog.shows.get(sid) or {}).get("title", "")
        m = _read()
        rec = _rec(m, title)
        pics = self._pics(title, rec)
        gone = [x for x in pics if not file or x["file"] == file]
        tried = set(rec.get("tried") or [])
        for x in gone:
            try:
                os.remove(DIR / x["file"])
            except OSError:
                pass
            if x.get("id"):
                tried.add(x["id"])
        left = [x for x in rec["pics"] if x["file"] not in {g["file"] for g in gone}]
        m[title] = {**rec, "pics": left, "tried": sorted(x for x in tried if x), "none": not left,
                    "removed": not left or rec.get("removed", False)}
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
                title = s.get("title", "")
                if title not in m:
                    return True  # never looked for
                rec = _rec(m, title)
                if rec.get("removed") or only_new:
                    return False
                found = [x for x in self._pics(title, rec) if not x.get("own")]
                # fewer than WANT found, or nothing: look again once with each newer search
                return len(found) < WANT and int(rec.get("v") or 1) < SEARCH_V
            todo = [sid for sid, s in self.app.catalog.shows.items() if wanted(s)]
            self.progress["total"] = len(todo)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "Rerun"}) as c:
                for i, sid in enumerate(todo):
                    title = self.app.catalog.shows[sid].get("title", "")
                    self.progress.update(step=title, done=i)
                    try:
                        self.progress["found"] += len(await self.for_show(c, sid))
                    except PermissionError as e:
                        self.progress["error"] = str(e)
                        return
                    except (httpx.HTTPError, ValueError, OSError) as e:
                        log.info("Couldn't get a cut-out for %s: %s", title, e)
                    await asyncio.sleep(0.3)  # gently
            self.progress["done"] = self.progress["total"]
            log.info("Character cut-outs: found %s for %s shows", self.progress["found"], self.progress["total"])
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
