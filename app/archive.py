"""Classic commercials from the Internet Archive.

People have uploaded thousands of VHS recordings of real commercial breaks to archive.org. For each
network you pick, Rerun searches for commercial, promo and bumper tapes from the years you want,
keeps the ones whose titles clearly match (right network, right years, US airings, no full
episodes), downloads them up to a space limit, and then finds where each commercial starts and
ends: on US television a moment of black picture and silence sits between ads.

The tapes aren't cut up. Next to each one goes a small "clips" file listing where every commercial
sits, and the library scan turns each into its own commercial, filed under the network's tag (so
the Nickelodeon channel can play only Nick ads). Everything lives in Rerun's own data folder.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil

import httpx

from .store import DATA_DIR

log = logging.getLogger("rerun.archive")

OWN = DATA_DIR / "commercials"          # downloads go here; sub-folder = network tag
INDEX = OWN / "index.json"
UA = "Rerun/1.0 (personal home TV app; https://github.com/Groot36-yep/rerun)"
MAX_ITEM = 2_500_000_000                # skip single tapes bigger than this
KEEP_FREE = 3_000_000_000               # never fill the disk past this much free space
CLIP_MIN, CLIP_MAX = 4.5, 125.0         # commercials, promos and bumpers are within this
# Each era becomes a tag too, so "90s Cartoons" can play 90s ads from every network.
ERAS = {"70s": (1970, 1979), "80s": (1980, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
        "2010s": (2010, 2019), "2020s": (2020, 2029)}
_ERA_IN_NAME = re.compile(r"\b(?:19)?([5-9]0)'?s\b|\b(20[0-2]0)'?s\b", re.I)


# A tape whose title names a holiday holds that holiday's ads: they're filed under the holiday and only
# air in its season. A tape from October or December (no holiday named) is filed under the month:
# those breaks lean seasonal, so Rerun favours them in that month but plays them all year.
HOLIDAY_WORDS = {
    "halloween": re.compile(r"hallowe.?en|spooky|trick.or.treat|halloweentown|haunt", re.I),
    "christmas": re.compile(r"christmas|x-?mas|holiday|santa|yuletide|winter ?break|december 2[0-5]\b", re.I),
}
MONTHS = {"october": re.compile(r"\boct(ober|\.)?\b|\b10/\d{1,2}/", re.I),
          "december": re.compile(r"\bdec(ember|\.)?\b|\b12/\d{1,2}/", re.I)}


def holiday_of(title: str) -> str | None:
    return next((k for k, rx in HOLIDAY_WORDS.items() if rx.search(title or "")), None)


def month_of(title: str) -> str | None:
    return next((k for k, rx in MONTHS.items() if rx.search(title or "")), None)


def era_tag(year) -> str | None:
    """2004 -> "2000s", 1997 -> "90s"."""
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return None
    return next((e for e, (a, b) in ERAS.items() if a <= y <= b), None)


def tag_for_network(name: str) -> str | None:
    """The commercials tag for a show's network: "Disney Channel" -> "disney"."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for key, (label, must, never, chans, _q) in PROFILES.items():
        if n == label.lower() or n in (c.lower() for c in chans):
            return key
    for key, (label, must, never, chans, _q) in PROFILES.items():
        if re.search(must, n, re.I) and not (never and re.search(never, n, re.I)):
            return key
    return None


def era_in_name(name: str) -> str | None:
    """ "90s Cartoons" -> "90s", "2000s Hits" -> "2000s"."""
    m = _ERA_IN_NAME.search(name or "")
    if not m:
        return None
    return f"{m.group(1)}s" if m.group(1) else f"{m.group(2)}s"

# key: (label, title must match, title must not match, Rerun channel names it belongs to, search words)
PROFILES = {
    "nick": ("Nickelodeon", r"\bnick(elodeon)?\b",
             r"\bnick[\s.]*jr\b|nickjr|noggin|nicktoons|nick at nite|teen ?nick|\bgas\b", ["Nickelodeon"],
             '"nickelodeon" OR nick'),
    "nickjr": ("Nick Jr.", r"\bnick[\s.]*jr\b|nickjr", r"", ["Nick Jr.", "Nick Jr"], '"nick jr" OR nickjr'),
    "nicktoons": ("Nicktoons", r"\bnicktoons\b", r"", ["Nicktoons"], "nicktoons"),
    "disney": ("Disney Channel", r"\bdisney channel\b", r"playhouse disney|toon disney|disney junior|disney xd",
               ["Disney Channel"], '"disney channel"'),
    "playhouse": ("Playhouse Disney", r"playhouse disney", r"", ["Playhouse Disney", "Disney Junior"],
                  '"playhouse disney"'),
    "toondisney": ("Toon Disney", r"toon disney", r"", ["Toon Disney", "Disney XD"], '"toon disney"'),
    "cn": ("Cartoon Network", r"cartoon network", r"adult swim|boomerang", ["Cartoon Network"],
           '"cartoon network"'),
    "adultswim": ("Adult Swim", r"adult swim", r"", ["Adult Swim"], '"adult swim"'),
    "boomerang": ("Boomerang", r"\bboomerang\b", r"", ["Boomerang"], "boomerang"),
    "kidswb": ("Kids' WB", r"kids'? ?wb", r"", ["Kids' WB", "The WB"], '"kids wb" OR "kids\' wb"'),
    "foxkids": ("Fox Kids", r"fox kids", r"", ["Fox Kids"], '"fox kids"'),
    "abcfamily": ("ABC Family / Fox Family", r"abc family|fox family", r"", ["ABC Family", "Freeform", "Fox Family"],
                  '"abc family" OR "fox family"'),
    "mtv": ("MTV", r"\bmtv\b", r"mtv ?2|mtv ?uk|mtv europe|mtv base", ["MTV"], "mtv"),
    "pbskids": ("PBS Kids", r"pbs kids", r"", ["PBS Kids"], '"pbs kids"'),
}
_FOREIGN = re.compile(r"\b(uk|u\.k\.|british|latin|latino|latinoam[eé]rica|brasil|brazil|españa|espa[nñ]ol|spanish|"
                      r"germany|deutsch|france|french|italia|italian|asia|india|pakistan|japan|australia|canada|"
                      r"canadian|europe|polska|poland|russia|arabia|middle east|philippines|portugal|nederland|"
                      r"scandinavia|nordic|korea|china|taiwan|mexico|argentina)\b", re.I)
_ADS = re.compile(r"commercial|\bads?\b|advert|promos?\b|bump(er)?s?\b|idents?\b|continuity|\bbreaks?\b", re.I)
_SURE = re.compile(r"commercials? only|commercial collection|commercial breaks?|^tv commercials|commercials,? "
                   r"(&|and) bump|promos? (&|and) bump", re.I)
_SHOWS = re.compile(r"\bepisodes?\b|programming|\bmovie\b|\bmarathon\b", re.I)  # fine if clearly ads-only
_NEVER = re.compile(r"with commercials|w/ ?commercials|original airing|full episodes?|vhs recording|full tape|"
                    r"soap opera|whole (show|episode)", re.I)  # always has the show in it
_RIPS = ("youtube-", "yt-", "dailymotion-", "videoplayback", "youcut", "capture-", "stream")
VIDEO_FORMATS = ("h.264", "h.264 ia", "mpeg4", "512kb mpeg4", "matroska", "quicktime", "mpeg2", "mpeg1",
                 "windows media", "avi", "ogg video", "webm")


def _years(text: str) -> set[int]:
    """Every year a title or description mentions, including ranges like 2003-2005."""
    found = set()
    for a, b in re.findall(r"\b(19[5-9]\d|20[0-2]\d)(?:\s*[-–]\s*(19[5-9]\d|20[0-2]\d))?\b", text or ""):
        a = int(a)
        found.update(range(a, int(b) + 1) if b and int(b) >= a else [a])
    for yy in re.findall(r"\b\d{1,2}/\d{1,2}/(\d{2})\b", text or ""):  # 9/20/02
        y = int(yy)
        found.add(2000 + y if y < 30 else 1900 + y)
    return found


def judge(key: str, doc: dict, y0: int, y1: int) -> tuple[float, str]:
    """How good a match an archive.org item is (0 = no), and why."""
    label, must, never, _chans, _q = PROFILES[key]
    title = doc.get("title") or ""
    if isinstance(title, list):
        title = " ".join(title)
    if not re.search(must, title, re.I):
        return 0, "different network"
    if never and re.search(never, title, re.I):
        return 0, "a different channel of the same family"
    if _FOREIGN.search(title):
        return 0, "not the US channel"
    if not _ADS.search(title):
        return 0, "not commercials"
    if _NEVER.search(title) or (_SHOWS.search(title) and not _SURE.search(title)):
        return 0, "has shows in it"
    years = _years(title)
    if not years:
        desc = doc.get("description") or ""
        years = _years(" ".join(desc) if isinstance(desc, list) else desc)
    if not years and not doc.get("identifier", "").lower().startswith(_RIPS) and doc.get("year"):
        years = {int(str(doc["year"])[:4])}
    if not years:
        return 0, "no year given"
    if not any(y0 <= y <= y1 for y in years):
        return 0, "outside your years"
    size = int(doc.get("item_size") or 0)
    if size > MAX_ITEM * 3:
        return 0, "too big"
    score = 1 + math.log10(1 + int(doc.get("downloads") or 0))
    score += 4 if _SURE.search(title) else 0
    score += 1 if re.search(r"bump|promo|ident", title, re.I) else 0
    score += 1 if len(years) <= 2 else 0          # a clearly dated tape beats a "1990s and 2000s" mix
    score -= 2 if size > MAX_ITEM else 0
    return score, "match"


def _title(doc: dict) -> str:
    t = doc.get("title") or ""
    return " ".join(t) if isinstance(t, list) else t


def _pick_files(files: list[dict]) -> list[dict]:
    """The video files to download from an item: the archive's own MP4 copy where there is one
    (smaller, plays everywhere), otherwise the uploaded file."""
    videos = [f for f in files if str(f.get("format", "")).lower() in VIDEO_FORMATS]
    by_original = {}
    for f in videos:
        if f.get("source") == "derivative" and f.get("original"):
            by_original.setdefault(f["original"], []).append(f)
    out = []
    for f in videos:
        if f.get("source") != "original":
            continue
        derived = [d for d in by_original.get(f["name"], []) if str(d.get("format", "")).lower() in ("h.264", "mpeg4",
                                                                                                    "h.264 ia")]
        best = min(derived, key=lambda d: int(d.get("size") or 0)) if derived else f
        if 0 < int(best.get("size") or 0) <= MAX_ITEM:
            out.append(best)
    return out


def find_clips(ffmpeg: str, path: str) -> list[tuple[float, float]]:
    """Where each commercial sits in a tape: (start, length) pairs, from the black-and-silent gaps."""
    import subprocess
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", path,
           "-vf", "scale=192:-2,blackdetect=d=0.05:pic_th=0.92:pix_th=0.12",
           "-af", "silencedetect=noise=-42dB:d=0.15", "-f", "null", "-"]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "15", *cmd]
    err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    s_starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    s_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    silences = list(zip(s_starts, s_ends))
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    if not total or not blacks:
        return []

    def quiet(a, b):
        return any(s - 0.4 <= b and e + 0.4 >= a for s, e in silences)

    strong = [(a + b) / 2 for a, b in blacks if quiet(a, b)]
    every = [(a + b) / 2 for a, b in blacks]

    def segments(cuts):
        cuts = sorted(cuts)
        merged = []
        for c in cuts:
            if not merged or c - merged[-1] > 3.0:
                merged.append(c)
        # the first and last pieces of a tape are usually cut off part-way, so they're left out
        return [(a, b - a) for a, b in zip(merged, merged[1:])]

    segs = segments(strong)
    if len(segs) < 3 or sorted(d for _, d in segs)[len(segs) // 2] > 90:
        segs = segments(every)  # no sound gaps on this tape; black frames alone will do
    return [(round(s + 0.05, 2), round(d - 0.1, 2)) for s, d in segs if CLIP_MIN <= d <= CLIP_MAX]


class Finder:
    """Searches, downloads and splits; one run at a time, in the background."""

    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None
        self.stop = False
        self.progress = {"running": False, "step": "", "done": 0, "total": 0, "found": [], "error": ""}

    # ---- what's here --------------------------------------------------------------------
    def index(self) -> dict:
        try:
            return json.loads(INDEX.read_text())
        except (OSError, ValueError):
            return {}

    def _save_index(self, idx: dict):
        OWN.mkdir(parents=True, exist_ok=True)
        tmp = INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=1))
        os.replace(tmp, INDEX)

    def summary(self) -> dict:
        idx = self.index()
        names = {c.get("auto_key", "")[8:].lower() for c in self.app.cfg.data["channels"]} | {
            c["name"].lower() for c in self.app.cfg.data["channels"]}
        out = []
        for key, (label, _m, _n, chans, _q) in PROFILES.items():
            got = idx.get(key, [])
            out.append({"key": key, "label": label, "have_channel": any(c.lower() in names for c in chans),
                        "tapes": len(got), "clips": sum(t.get("clips", 0) for t in got),
                        "bytes": sum(t.get("bytes", 0) for t in got),
                        "eras": {e: sum(t.get("clips", 0) for t in got if t.get("era") == e) for e in ERAS
                                 if any(t.get("era") == e for t in got)},
                        "holidays": {h: sum(t.get("clips", 0) for t in got if t.get("holiday") == h)
                                     for h in HOLIDAY_WORDS if any(t.get("holiday") == h for t in got)},
                        "titles": [t["title"] for t in got][:12]})
        wanted = sorted({e for c in self.app.cfg.data["channels"] if (e := era_in_name(c["name"]))},
                        key=list(ERAS).index)
        return {"networks": out, "eras": list(ERAS), "channel_eras": wanted, "holidays": list(HOLIDAY_WORDS),
                "status": self.progress}

    # ---- a run --------------------------------------------------------------------------
    def start(self, keys: list[str], eras: list[str], budget: int, holidays: list[str] | None = None):
        if self.task and not self.task.done():
            raise RuntimeError("Already looking for commercials")
        keys = [k for k in keys if k in PROFILES]
        eras = [e for e in eras if e in ERAS]
        if not keys:
            raise ValueError("Choose at least one network")
        if not eras:
            raise ValueError("Choose at least one era")
        self.stop = False
        self.progress = {"running": True, "step": "Starting…", "done": 0, "total": 0, "found": [], "error": ""}
        holidays = [h for h in (holidays or []) if h in HOLIDAY_WORDS]
        self.task = asyncio.create_task(self._run(keys, eras, budget, holidays))

    def _step(self, text: str, **kw):
        self.progress.update(step=text, **kw)

    async def _run(self, keys, eras, budget, holidays=()):
        try:
            self._register_folder()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": UA}) as c:
                for key in keys:
                    docs = None
                    for era in eras:  # the space for a network is shared between the eras picked
                        if self.stop:
                            break
                        if docs is None:
                            docs = await self._search(c, key)
                        await self._network(c, key, era, docs, budget // len(eras))
                    for hol in holidays:  # holiday tapes get their own share of space
                        hdocs = await self._search(c, key, hol)
                        for era in eras:
                            if self.stop:
                                break
                            await self._network(c, key, era, hdocs, budget // len(eras) // 2, hol)
            self._step("Adding them to your channels…")
            self._assign_tags(keys, eras)
            await self._rescan()
            self._step("Done. Your library is being rescanned so channels pick them up.", running=False)
        except Exception as e:
            log.exception("Commercial finder failed")
            self.progress.update(running=False, error=f"Something went wrong: {e}", step="")
        finally:
            self.progress["running"] = False

    async def _search(self, c, key, holiday: str | None = None) -> list[dict]:
        label, _m, _n, _ch, words = PROFILES[key]
        self._step(f"Searching the Internet Archive for {label}{' ' + holiday if holiday else ''}…")
        extra = {"halloween": " AND title:(halloween OR spooky OR \"trick or treat\" OR october)",
                 "christmas": " AND title:(christmas OR xmas OR holiday OR santa OR december)"}.get(holiday, "")
        q = (f"title:({words}) AND title:(commercial* OR promo* OR bumper* OR bumps OR ads OR idents OR break*)"
             f"{extra} AND mediatype:movies")
        r = await c.get("https://archive.org/advancedsearch.php", params={
            "q": q, "fl[]": ["identifier", "title", "year", "downloads", "item_size", "description"],
            "rows": 500, "output": "json", "sort[]": "downloads desc"})
        r.raise_for_status()
        return r.json()["response"]["docs"]

    async def _network(self, c, key, era, docs, budget, holiday: str | None = None):
        label = PROFILES[key][0]
        y0, y1 = ERAS[era]
        scored = [(judge(key, d, y0, y1)[0], d) for d in docs]
        matches = [d for sc, d in sorted(scored, key=lambda x: -x[0]) if sc > 0]
        if holiday:  # a holiday run only wants tapes that are clearly that holiday's
            matches = [d for d in matches if holiday_of(_title(d)) == holiday
                       or (holiday == "halloween" and month_of(_title(d)) == "october")
                       or (holiday == "christmas" and month_of(_title(d)) == "december")]
        idx = self.index()
        have = {t["id"] for t in idx.get(key, [])}
        used = sum(t.get("bytes", 0) for t in idx.get(key, [])
                   if t.get("era") == era and bool(t.get("holiday")) == bool(holiday))
        what = f"{label} {holiday + ' ' if holiday else ''}{era}"
        log.info("Commercial finder: %d %s tapes", len(matches), what)
        self.progress["found"].append({"network": what, "matches": len(matches)})
        for doc in matches:
            if self.stop or used >= budget:
                break
            if doc["identifier"] in have:
                continue
            meta = (await c.get(f"https://archive.org/metadata/{doc['identifier']}")).json()
            files = _pick_files(meta.get("files", []))
            size = sum(int(f.get("size") or 0) for f in files)
            if not files or used + size > budget * 1.15:
                continue
            if shutil.disk_usage(DATA_DIR).free - size < KEEP_FREE:
                self.progress["error"] = "Stopped: the disk is nearly full."
                self.stop = True
                break
            title = _title(doc) or doc["identifier"]
            # folder names become tags: "nick", "90s", and "halloween" (holiday ads, only in season)
            # or "october" (an October tape: favoured in October, played all year)
            season = holiday_of(title) or month_of(title)
            folder = OWN / key / era / season if season else OWN / key / era
            tape = {"id": doc["identifier"], "title": title, "era": era, "files": [], "clips": 0, "bytes": 0,
                    "years": sorted(_years(title)), "holiday": holiday_of(title) or "", "month": month_of(title) or ""}
            saved = []
            for f in files:
                if self.stop:
                    break
                dest = folder / f"{doc['identifier'][:60]}--{os.path.basename(f['name'])}"
                self._step(f"{label}: downloading “{title[:60]}”", done=0, total=int(f.get("size") or 0))
                await self._download(c, f"https://archive.org/download/{doc['identifier']}/{f['name']}", dest)
                self._step(f"{label}: finding the commercials in “{title[:60]}”", done=0, total=0)
                clips = await asyncio.to_thread(find_clips, self.app.ffmpeg.ffmpeg, str(dest))
                if not clips:
                    log.info("No clear commercial breaks found in %s; skipping it", dest.name)
                    dest.unlink(missing_ok=True)
                    continue
                (dest.parent / (dest.name + ".clips.json")).write_text(json.dumps(
                    {"v": 1, "source": f"https://archive.org/details/{doc['identifier']}", "title": title,
                     "clips": clips}))
                tape["files"].append(f["name"])
                saved.append(dest)
                tape["clips"] += len(clips)
                tape["bytes"] += dest.stat().st_size
            if tape["files"]:
                idx = self.index()
                idx.setdefault(key, []).append(tape)
                self._save_index(idx)
                used += tape["bytes"]
                log.info("Added %d %s commercials from “%s”", tape["clips"], label, title)
            else:
                for d in saved:
                    d.unlink(missing_ok=True)
                    (d.parent / (d.name + ".clips.json")).unlink(missing_ok=True)

    async def _download(self, c, url: str, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        for attempt in range(3):
            try:
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(part, "wb") as out:
                        async for chunk in r.aiter_bytes(1 << 20):
                            if self.stop:
                                raise asyncio.CancelledError
                            out.write(chunk)
                            self.progress["done"] = self.progress.get("done", 0) + len(chunk)
                os.replace(part, dest)
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
                await asyncio.sleep(5 * (attempt + 1))
            except asyncio.CancelledError:
                part.unlink(missing_ok=True)
                raise RuntimeError("Stopped") from None

    def _register_folder(self):
        src = {"type": "folder", "path": str(OWN)}
        lst = self.app.cfg.data["sources"]["commercials"]
        if not any(s.get("type") == "folder" and s.get("path") == str(OWN) for s in lst):
            lst.append(src)
            self.app.cfg.save()
        OWN.mkdir(parents=True, exist_ok=True)

    def _assign_tags(self, keys: list[str], eras: list[str]):
        """Network channels play their own network's commercials; decade channels ("90s Cartoons")
        play that decade's, from every network."""
        idx = self.index()
        got_eras = {t.get("era") for tapes in idx.values() for t in tapes}
        changed = False

        def give(ch, tag):
            nonlocal changed
            com = ch.setdefault("commercials", {})
            tags = com.setdefault("tags", [])
            if tag not in tags:
                tags.append(tag)
                com["enabled"] = True
                changed = True

        for ch in self.app.cfg.data["channels"]:
            names = {ch["name"].lower(), ch.get("auto_key", "")[8:].lower()}
            for key in keys:
                if idx.get(key) and any(n.lower() in names for n in PROFILES[key][3]):
                    give(ch, key)
            era = era_in_name(ch["name"])
            if era and era in eras and era in got_eras:
                give(ch, era)
        if changed:
            self.app.cfg.save()

    def remove(self, key: str):
        idx = self.index()
        idx.pop(key, None)
        self._save_index(idx)
        shutil.rmtree(OWN / key, ignore_errors=True)
        for ch in self.app.cfg.data["channels"]:
            tags = (ch.get("commercials") or {}).get("tags") or []
            if key in tags:
                tags.remove(key)
        self.app.cfg.save()
        asyncio.ensure_future(self._rescan())

    async def _rescan(self):
        """Scan the library so channels pick up the new commercials (after any scan already running)."""
        while self.app.scanner.progress.get("running"):
            await asyncio.sleep(5)
        self.app.scanner.start(manual=True)
