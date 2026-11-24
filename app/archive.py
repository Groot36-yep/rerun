"""Classic commercials from the Internet Archive.

People have uploaded thousands of recordings of real commercial breaks to archive.org. For each network
you pick, Rerun searches for commercial, promo and bumper tapes, and for "airing tapes" (a whole
broadcast recorded with its original commercials). It keeps the ones whose titles clearly match (right
network, right years, US airings), and ranks them by the real quality of their files, not by what the
title claims: picture size, bitrate, frame rate, and a check for videos that were blown up from a
smaller picture.

On US television a moment of black picture and silence sits between ads. Rerun finds those, lines the
pieces up with real ad lengths (10, 15, 30, 60 seconds...), and for airing tapes keeps only the breaks
and none of the show. The same commercial often turns up on several tapes; Rerun keeps the best-looking
copy on the air.

Commercial tapes aren't cut up. Next to each goes a small "clips" file listing where every commercial
sits, and the library scan turns each into its own commercial, filed under the network's tag (so the
Nickelodeon channel can play only Nick ads). From airing tapes only the breaks are saved, and Rerun
remembers where in the show each break fell, so it can play them back at the same places.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import shutil
import subprocess

import httpx

from .store import DATA_DIR

log = logging.getLogger("rerun.archive")

OWN = DATA_DIR / "commercials"          # downloads go here; sub-folder = network tag
INDEX = OWN / "index.json"
PRINTS = OWN / "fingerprints.json"
AIRINGS = OWN / "airings.json"
UA = "Rerun/1.0 (personal home TV app; https://github.com/Groot36-yep/rerun)"
MAX_ITEM = 2_500_000_000                # skip single commercial tapes bigger than this
MAX_AIRING = 25_000_000_000             # airing tapes are read over the network, never stored whole
KEEP_FREE = 3_000_000_000               # never fill the disk past this much free space
CLIP_MIN, CLIP_MAX = 4.5, 125.0         # commercials, promos and bumpers are within this
STANDARD = (5, 10, 15, 20, 30, 45, 60, 90, 120)  # lengths ads were sold in
STD_SLACK = 0.8
UPSCALED_PSNR = 43.0                    # see effective_height()
# Each era becomes a tag too, so "90s Cartoons" can play 90s ads from every network.
ERAS = {"70s": (1970, 1979), "80s": (1980, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
        "2010s": (2010, 2019), "2020s": (2020, 2029)}
_ERA_IN_NAME = re.compile(r"\b(?:19)?([5-9]0)'?s\b|\b(20[0-2]0)'?s\b", re.I)


# A tape whose title names a holiday holds that holiday's ads: they're filed under the holiday and only
# air in its season. A tape from October or December (no holiday named) is filed under the month:
# those breaks lean seasonal, so Rerun favours them in that month but plays them all year.
HOLIDAY_WORDS = {
    "halloween": re.compile(r"hallowe.?en|spooky|trick.or.treat|halloweentown|haunt", re.I),
    "christmas": re.compile(r"christmas|x-?mas|holiday|santa|yuletide|winter ?break|december 2[0-5]\b|"
                            r"25 days of", re.I),
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
    "abcfamily": ("ABC Family / Fox Family", r"abc family|fox family|25 days of christmas", r"freeform",
                  ["ABC Family", "Freeform", "Fox Family"], '"abc family" OR "fox family" OR "25 days of christmas"'),
    "mtv": ("MTV", r"\bmtv\b", r"mtv ?2|mtv ?uk|mtv europe|mtv base", ["MTV"], "mtv"),
    "pbskids": ("PBS Kids", r"pbs kids", r"", ["PBS Kids"], '"pbs kids"'),
}
_FOREIGN = re.compile(r"\b(uk|u\.k\.|british|latin|latino|latinoam[eé]rica|brasil|brazil|españa|espa[nñ]ol|spanish|"
                      r"germany|german|deutsch|france|french|italy|italia|italian|spain|asia|india|pakistan|japan|australia|australian|"
                      r"canada|canadian|europe|polska|poland|russia|arabia|middle east|philippines|portugal|nederland|"
                      r"scandinavia|nordic|korea|china|taiwan|mexico|argentina|gtv|atv|sbs|itv|ctv|cbc|ytv|"
                      r"teletoon|treehouse)\b", re.I)
_ADS = re.compile(r"commercial|\bads?\b|advert|promos?\b|promotional|bump(er)?s?\b|idents?\b|continuity|\bbreaks?\b",
                  re.I)
# A collection of bumpers and idents: welcome even without a year (they ran for years and never date).
_BUMPS = re.compile(r"\bbump(er)?s?\b|\bidents?\b|you'?re watching|coming up next|\bnext\b.*\blater\b|continuity", re.I)
_SURE = re.compile(r"commercials? only|commercial collection|commercial breaks?|^tv commercials|commercials,? "
                   r"(&|and) bump|promos? (&|and) bump", re.I)
_SHOWS = re.compile(r"\bepisodes?\b|programming|\bmovie\b|\bmarathon\b", re.I)  # fine if clearly ads-only
# A whole broadcast with its commercials: the show is left out and only the breaks are kept.
_AIRING = re.compile(r"with (original )?commercials|w/ ?(original )?commercials|\bw\.?o\.?c\b\.?|original airing|"
                     r"airing tape|full broadcast|broadcast with|commercials included|with ads\b|\bairing\b|"
                     r"vhs recording|full tape|tv recording", re.I)
_NEVER = re.compile(r"soap opera|(commercials?|commercial breaks?|ads|breaks) (were |are |have been )?"
                    r"(not recorded|not included|removed|cut out|edited out|skipped|paused out)|"
                    r"without (the )?(commercials|ads)|no commercials|commercial[- ]free|commercials? (cut|removed)",
                    re.I)
_BETTER = re.compile(r"\b(60 ?fps|50 ?fps|dvd|broadcast master|master|hd\b|720p|1080p|betacam|digital|"
                     r"high quality|remaster|s-?vhs|direct feed|first generation|1st gen)", re.I)
_WORSE = re.compile(r"\b(low quality|bad quality|poor quality|messed up|incomplete|partial|240p|144p|"
                    r"damaged|tracking|ep mode|slp)", re.I)
_RIPS = ("youtube-", "yt-", "dailymotion-", "videoplayback", "youcut", "capture-", "stream")
VIDEO_FORMATS = ("h.264", "h.264 ia", "mpeg4", "512kb mpeg4", "matroska", "quicktime", "mpeg2", "mpeg1",
                 "windows media", "avi", "ogg video", "webm")


def _years(text: str) -> set[int]:
    """Every year a title or description mentions, including ranges like 2003-2005."""
    found = set()
    for a, b in re.findall(r"\b(19[5-9]\d|20[0-2]\d)(?:\s*[-–]\s*(19[5-9]\d|20[0-2]\d))?\b", text or ""):
        a = int(a)
        found.update(range(a, int(b) + 1) if b and int(b) >= a else [a])
    for yy in re.findall(r"\b\d{1,2}[/.-]\d{1,2}[/.-](\d{2})\b", text or ""):  # 9/20/02, 12-2-09
        y = int(yy)
        found.add(2000 + y if y < 30 else 1900 + y)
    return found


def _text(v) -> str:
    return " ".join(v) if isinstance(v, list) else str(v or "")


def _title(doc: dict) -> str:
    return _text(doc.get("title"))


def tape_years(doc: dict) -> set[int]:
    """The years a tape was recorded, from its title, then its description, then its date."""
    years = _years(_title(doc))
    if not years:
        years = _years(_text(doc.get("description")))
    if not years and not doc.get("identifier", "").lower().startswith(_RIPS):
        for f in ("date", "year"):
            if doc.get(f):
                m = re.match(r"(19|20)\d\d", str(doc[f]))
                if m:
                    years = {int(m.group(0))}
                    break
    return years


def judge(key: str, doc: dict, y0: int, y1: int, undated_bumpers: bool = False) -> tuple[float, str, str]:
    """How good a match an archive.org item is (0 = no), why, and what it is: "ads" (a tape of
    commercials), "airing" (a broadcast with its commercials, whose breaks get cut out) or "bumps" (a
    collection of bumpers and idents with no date, accepted when `undated_bumpers` is on)."""
    label, must, never, _chans, _q = PROFILES[key]
    title = _title(doc)
    desc = _text(doc.get("description"))
    if not re.search(must, title, re.I):
        return 0, "different network", ""
    if never and re.search(never, title, re.I):
        return 0, "a different channel of the same family", ""
    if _FOREIGN.search(title):
        return 0, "not the US channel", ""
    if _NEVER.search(title) or _NEVER.search(desc[:2000]):
        return 0, "has no commercials in it", ""
    if _AIRING.search(title):
        kind = "airing"
    elif _ADS.search(title) and (not _SHOWS.search(title) or _SURE.search(title)):
        kind = "ads"
    else:
        return 0, "not commercials", ""
    years = tape_years(doc)
    if not years and undated_bumpers and kind == "ads" and _BUMPS.search(title):
        score = 2 + math.log10(1 + int(doc.get("downloads") or 0)) + (2 if _BETTER.search(title) else 0)
        return score, "bumpers", "bumps"
    if not years:
        return 0, "no year given", ""
    if not any(y0 <= y <= y1 for y in years):
        return 0, "outside your years", ""
    size = int(doc.get("item_size") or 0)
    if size > (MAX_AIRING if kind == "airing" else MAX_ITEM * 3):
        return 0, "too big", ""
    score = 1 + math.log10(1 + int(doc.get("downloads") or 0))
    score += 4 if _SURE.search(title) else 0
    score += 1 if re.search(r"bump|promo|ident", title, re.I) else 0
    score += 1 if len(years) <= 2 else 0          # a clearly dated tape beats a "1990s and 2000s" mix
    score += 2 if _BETTER.search(title) else 0
    score -= 3 if _WORSE.search(title) else 0
    score -= 1 if kind == "airing" else 0         # fewer ads per gigabyte, so plain ad tapes go first
    score -= 2 if kind == "ads" and size > MAX_ITEM else 0
    return score, "match", kind


# ---- picture quality --------------------------------------------------------------------------

def file_quality(f: dict) -> dict:
    """What archive.org says about a file: size, bitrate. Heights it doesn't know are 0."""
    w, h = int(f.get("width") or 0), int(f.get("height") or 0)
    try:
        length = float(f.get("length") or 0)
    except ValueError:  # "12:34" style
        parts = [float(p) for p in str(f.get("length")).split(":") if p.replace(".", "").isdigit()]
        length = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    kbps = int(int(f.get("size") or 0) * 8 / length / 1000) if length else 0
    return {"w": w, "h": h, "kbps": kbps, "fps": 0, "len": length}


def quality_score(q: dict | None) -> float:
    """0 (unwatchable) to 100 (crisp HD), from size, bits per pixel and smoothness. Uses the
    effective height ("eh") when Rerun has checked whether the video was blown up."""
    if not q:
        return 0.0
    h = min(int(q.get("eh") or q.get("h") or 0), 1080)
    w = int(q.get("w") or 0) or int(h * 4 / 3)
    if not h:
        return 0.0
    fps = float(q.get("fps") or 0) or 29.97
    score = 55 * min(h, 720) / 720 + (10 if h >= 1000 else 0)
    if q.get("kbps"):
        bpp = q["kbps"] * 1000 / max(1, w * int(q.get("h") or h) * fps)
        score += 25 * min(1.0, bpp / 0.12)   # starved files are blocky whatever their size
    else:
        score += 10
    score += 10 if fps >= 49 else 0          # smooth 60 fps transfers look like real TV
    return round(min(100.0, score), 1)


def quality_label(q: dict | None) -> str:
    h = int((q or {}).get("eh") or (q or {}).get("h") or 0)
    return "HD" if h >= 700 else "Good" if h >= 400 else "Watchable" if h >= 300 else "Low" if h else "?"


def _ffprobe(ffmpeg: str) -> str:
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if os.path.dirname(ffmpeg) else "ffprobe"
    if os.name == "nt" and os.path.dirname(ffmpeg):
        probe += ".exe"
    return probe if (shutil.which(probe) or os.path.exists(probe)) else "ffprobe"


def probe_video(ffmpeg: str, path: str) -> dict:
    """Picture size, frame rate and bitrate of a video file."""
    out = subprocess.run([_ffprobe(ffmpeg), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,avg_frame_rate:format=bit_rate,duration", "-of", "json", path],
                         capture_output=True, text=True, errors="replace").stdout
    try:
        d = json.loads(out or "{}")
    except ValueError:
        d = {}
    s = (d.get("streams") or [{}])[0]
    fmt = d.get("format") or {}
    try:
        num, den = (s.get("avg_frame_rate") or "0/1").split("/")
        fps = round(int(num) / int(den), 2) if int(den) else 0
    except ValueError:
        fps = 0
    return {"w": int(s.get("width") or 0), "h": int(s.get("height") or 0), "fps": fps,
            "kbps": int(int(fmt.get("bit_rate") or 0) / 1000), "len": float(fmt.get("duration") or 0)}


def video_height(ffmpeg: str, path: str) -> int:
    """The picture height of a video (480 for 480p), or 0 if it can't be told."""
    return probe_video(ffmpeg, path)["h"]


def effective_height(ffmpeg: str, path: str, height: int, at: float = 0, length: float = 20) -> int:
    """The height a video really has. Many uploads were blown up from a smaller picture (a 240-line
    capture saved as 480p or 1080p): halving such a picture's height and scaling it back loses nothing,
    while a real picture loses fine detail. Only the height is tested, because VHS is soft side to side
    but keeps its full height."""
    eff = height
    for _ in range(3):
        if eff < 300:
            break
        cmd = [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{at:.1f}", "-i", path, "-t", f"{length:.1f}",
               "-filter_complex",
               f"[0:v]scale=-2:{eff},split[a][b];[b]scale=iw:trunc(ih/4)*2:flags=area,"
               f"scale=iw:{eff}:flags=bicubic[c];[a][c]psnr", "-f", "null", "-"]
        err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
        m = re.search(r"PSNR y:([\d.]+|inf)", err)
        if not m:
            break
        psnr = 99.0 if m.group(1) == "inf" else float(m.group(1))
        if psnr < UPSCALED_PSNR:
            break
        eff //= 2
    return eff


# ---- finding the commercials in a recording ---------------------------------------------------

def _http_args(src: str) -> list[str]:
    return ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"] if src.startswith("http") \
        else []


SHIFT = 1_000_000


def detect(ffmpeg: str, src: str) -> tuple[list[float], list[float], float]:
    """Cut points in a recording: (black-and-silent cuts, every black cut, length).

    Two black detectors run at once. Clearly black frames are a cut by themselves. On worn tapes the
    gap between ads is often only nearly black, so nearly-black frames count too, but only when the
    sound drops out at the same moment (dark scenes inside an ad keep their sound). The silence
    between ads on tape is short (a tenth of a second or two) and sits on top of tape hiss, hence the
    fairly loud threshold."""
    def run(with_sound: bool) -> subprocess.CompletedProcess:
        graph = ("[0:v:0]scale=192:-2,split[a][b];[a]blackdetect=d=0.05:pic_th=0.92:pix_th=0.12[x];"
                 "[b]setpts=PTS+1000000/TB,blackdetect=d=0.03:pic_th=0.85:pix_th=0.15[y]")
        outs = ["-map", "[x]", "-f", "null", "-", "-map", "[y]", "-f", "null", "-"]
        if with_sound:
            graph += ";[0:a:0]silencedetect=noise=-36dB:d=0.06[z]"
            outs += ["-map", "[z]", "-f", "null", "-"]
        cmd = [ffmpeg, "-hide_banner", "-nostats", *_http_args(src), "-i", src, "-filter_complex", graph, *outs]
        if shutil.which("nice"):
            cmd = ["nice", "-n", "15", *cmd]
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace")

    res = run(True)
    if res.returncode != 0 and re.search(r"0:a:0|matches no streams|Stream specifier", res.stderr):
        res = run(False)  # a tape without sound: black frames alone
    err = res.stderr
    # Newer ffmpeg doesn't say which detector printed a line, so the loose one's clock runs a million
    # seconds ahead.
    strict, loose = [], []
    for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err):
        a, b = float(a), float(b)
        if a >= SHIFT:
            loose.append((a - SHIFT, b - SHIFT))
        else:
            strict.append((a, b))
    s_starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    s_ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    silences = list(zip(s_starts, s_ends))
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    if not total:  # streams without a duration header: the last timestamp ffmpeg reached
        times = re.findall(r"time=(\d+):(\d+):([\d.]+)", err)
        if times:
            hh, mm, ss = times[-1]
            total = int(hh) * 3600 + int(mm) * 60 + float(ss)

    def quiet(a, b):
        return any(s - 0.4 <= b and e + 0.4 >= a for s, e in silences)

    hushed = [(a, b) for a, b in loose if quiet(a, b)]
    strong = [(a + b) / 2 for a, b in strict + hushed if quiet(a, b)]
    every = [(a + b) / 2 for a, b in strict + hushed]
    return strong, every, total


def _merge(cuts: list[float]) -> list[float]:
    merged = []
    for c in sorted(cuts):
        if not merged or c - merged[-1] > 3.0:
            merged.append(c)
    return merged


def is_standard(d: float) -> bool:
    return any(abs(d - s) <= max(STD_SLACK, s * 0.035) for s in STANDARD)  # tapes run a little fast or slow


def snap(segs: list[tuple[float, float]]) -> list[dict]:
    """Join pieces that were cut by a fade inside a commercial: two or three neighbours that add up to
    a real ad length (15, 30, 60...) become one commercial."""
    out, i = [], 0
    while i < len(segs):
        s, d = segs[i]
        took = 1
        if not is_standard(d):
            total = d
            for k in range(1, 4):
                if i + k >= len(segs) or abs(segs[i + k][0] - (segs[i + k - 1][0] + segs[i + k - 1][1])) > 0.5:
                    break
                total += segs[i + k][1]
                if total > CLIP_MAX:
                    break
                if is_standard(total):
                    d, took = total, k + 1
                    break
        out.append({"s": s, "d": d, "std": is_standard(d)})
        i += took
    return out


def _pieces(cuts: list[float], total: float, ends: bool) -> list[tuple[float, float]]:
    cuts = _merge(cuts)
    bounds = ([0.0] if ends else []) + cuts + ([total] if ends and total else [])
    return [(a, b - a) for a, b in zip(bounds, bounds[1:]) if b > a]


def find_clips_ex(ffmpeg: str, src: str) -> list[dict]:
    """Where each commercial sits in a tape: [{"s": start, "d": length, "std": a real ad length}]."""
    strong, every, total = detect(ffmpeg, src)
    if not total or not every:
        return []
    # the first and last pieces of a tape are usually cut off part-way, so they're left out
    segs = _pieces(strong, total, ends=False)
    if len(segs) < 3 or sorted(d for _, d in segs)[len(segs) // 2] > 90:
        segs = _pieces(every, total, ends=False)  # no sound gaps on this tape; black frames alone will do
    clips = snap(segs)
    return [{"s": round(c["s"] + 0.05, 2), "d": round(c["d"] - 0.1, 2), "std": c["std"]}
            for c in clips if CLIP_MIN <= c["d"] <= CLIP_MAX]


def find_clips(ffmpeg: str, path: str) -> list[tuple[float, float]]:
    """(start, length) of each commercial in a tape (kept for the library scan's splitter)."""
    return [(c["s"], c["d"]) for c in find_clips_ex(ffmpeg, path)]


def breaks_in(segs: list[tuple[float, float]]) -> list[dict]:
    """The commercial breaks in a broadcast: runs of ad-length pieces between long stretches of show.
    A movie's own fades to black make the odd short piece too, so a break needs several pieces, most
    of them real ad lengths. Returns [{"s", "e", "at": seconds of show before it}]."""
    out, run, shown = [], [], 0.0

    def flush():
        nonlocal run, shown
        if run:
            n = len(run)
            std = sum(1 for _, d in run if is_standard(d)) / n
            total = sum(d for _, d in run)
            if total >= 20 and ((n >= 3 and std >= 0.4) or (n == 2 and std == 1.0)):
                out.append({"s": run[0][0], "e": run[-1][0] + run[-1][1], "at": round(shown, 1), "n": n})
            else:
                shown += total  # not a break: short scenes of the show itself
        run = []

    for s, d in segs:
        if CLIP_MIN <= d <= CLIP_MAX:
            run.append((s, d))
            continue
        flush()
        shown += d
    flush()
    return out


def find_breaks(ffmpeg: str, src: str) -> list[dict]:
    strong, every, total = detect(ffmpeg, src)
    if not total or not every:
        return []
    found = breaks_in(_pieces(strong, total, ends=True))
    if len(found) < 2:
        alt = breaks_in(_pieces(every, total, ends=True))
        if len(alt) > len(found):
            found = alt
    return found


def extract(ffmpeg: str, src: str, start: float, end: float, dest: str) -> bool:
    """Copy one break out of a recording (no re-encoding), with a little of the show either side so
    the first and last commercials keep their edges."""
    a = max(0.0, start - 3)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *_http_args(src), "-ss", f"{a:.2f}", "-i", src,
           "-t", f"{end - a + 3:.2f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
           "-avoid_negative_ts", "make_zero", "-f", "matroska", dest]
    ok = subprocess.run(cmd, capture_output=True).returncode == 0
    return ok and os.path.exists(dest) and os.path.getsize(dest) > 10000


# ---- telling copies of the same commercial apart ------------------------------------------------

def fingerprint(ffmpeg: str, path: str, start: float, dur: float) -> list[int]:
    """Three tiny pictures from inside a commercial (a "difference hash" of each), enough to spot the
    same ad on another tape even when the picture quality differs."""
    a, span = start + dur * 0.15, max(1.0, dur * 0.7)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{a:.2f}", "-i", path, "-t", f"{span:.2f}",
           "-vf", f"fps={3 / span:.5f},scale=9:8:flags=area,format=gray", "-frames:v", "3",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    out = []
    for k in range(len(raw) // 72):
        px = raw[k * 72:(k + 1) * 72]
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
        out.append(bits)
    return out


def same_ad(a: dict, b: dict) -> bool:
    if abs(a["d"] - b["d"]) > 1.2 or not a.get("h") or not b.get("h"):
        return False
    close = sum(1 for x, y in zip(a["h"], b["h"]) if bin(x ^ y).count("1") <= 10)
    return close >= min(2, len(a["h"]), len(b["h"]))


def ad_names(description) -> list[str]:
    """The list of commercials an uploader typed into a tape's description, in order."""
    text = _text(description)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>|</?(div|li|p)(\s[^>]*)?>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    names = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-•*–]|\d{1,3}[.)])\s*(\S.*)$", line)
        if m:
            name = m.group(1).strip()
            if 2 <= len(name) <= 90:
                names.append(name)
    return names


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class Finder:
    """Searches, downloads and splits; one run at a time, in the background."""

    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None
        self.stop = False
        self.progress = {"running": False, "step": "", "done": 0, "total": 0, "found": [], "error": "", "log": []}
        self.min_height = 0

    # ---- what's here --------------------------------------------------------------------
    def index(self) -> dict:
        try:
            return json.loads(INDEX.read_text())
        except (OSError, ValueError):
            return {}

    def _save_json(self, path, data):
        OWN.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    def _save_index(self, idx: dict):
        self._save_json(INDEX, idx)

    def airings(self) -> list[dict]:
        try:
            return json.loads(AIRINGS.read_text())
        except (OSError, ValueError):
            return []

    def summary(self) -> dict:
        idx = self.index()
        names = {c.get("auto_key", "")[8:].lower() for c in self.app.cfg.data["channels"]} | {
            c["name"].lower() for c in self.app.cfg.data["channels"]}
        out = []
        for key, (label, _m, _n, chans, _q) in PROFILES.items():
            got = idx.get(key, [])
            out.append({"key": key, "label": label, "have_channel": any(c.lower() in names for c in chans),
                        "tapes": len(got), "clips": sum(t.get("clips", 0) for t in got),
                        "dupes": sum(t.get("dupes", 0) for t in got),
                        "bytes": sum(t.get("bytes", 0) for t in got),
                        "eras": {e: sum(t.get("clips", 0) for t in got if t.get("era") == e) for e in ERAS
                                 if any(t.get("era") == e for t in got)},
                        "holidays": {h: sum(t.get("clips", 0) for t in got if t.get("holiday") == h)
                                     for h in HOLIDAY_WORDS if any(t.get("holiday") == h for t in got)},
                        "titles": [f"{t['title']} — {quality_label(t.get('q'))}" for t in got][:12]})
        wanted = sorted({e for c in self.app.cfg.data["channels"] if (e := era_in_name(c["name"]))},
                        key=list(ERAS).index)
        return {"networks": out, "eras": list(ERAS), "channel_eras": wanted, "holidays": list(HOLIDAY_WORDS),
                "status": self.progress}

    # ---- a run --------------------------------------------------------------------------
    def _begin(self, coro_fn, *args):
        if self.task and not self.task.done():
            raise RuntimeError("Already looking for commercials")
        self.stop = False
        self.progress = {"running": True, "step": "Starting…", "done": 0, "total": 0, "found": [], "error": "",
                         "log": []}
        self.task = asyncio.create_task(coro_fn(*args))

    def start(self, keys: list[str], eras: list[str], budget: int, holidays: list[str] | None = None,
              min_height: int = 0, airings: bool = True):
        keys = [k for k in keys if k in PROFILES]
        eras = [e for e in eras if e in ERAS]
        if not keys:
            raise ValueError("Choose at least one network")
        if not eras:
            raise ValueError("Choose at least one era")
        holidays = [h for h in (holidays or []) if h in HOLIDAY_WORDS]
        self.min_height = int(min_height or 0)
        self._begin(self._run, keys, eras, budget, holidays, airings)

    def _step(self, text: str, **kw):
        self.progress.update(step=text, **kw)

    def _note(self, text: str):
        log.info("Commercial finder: %s", text)
        self.progress["log"] = (self.progress.get("log", []) + [text])[-40:]

    async def _run(self, keys, eras, budget, holidays=(), airings=True):
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
                        await self._network(c, key, era, docs, budget // len(eras), None, airings)
                    if not self.stop:  # bumper and ident collections, which often carry no date
                        await self._bumpers(c, key, docs or await self._search(c, key), budget // 4)
                    for hol in holidays:  # holiday tapes get their own share of space
                        hdocs = await self._search(c, key, hol)
                        for era in eras:
                            if self.stop:
                                break
                            await self._network(c, key, era, hdocs, budget // len(eras) // 2, hol, airings)
            self._step("Adding them to your channels…")
            self._assign_tags(keys, eras)
            await self._rescan()
            self._step("Done. Your library is being rescanned so channels pick them up.", running=False)
        except Exception as e:
            log.exception("Commercial finder failed")
            self.progress.update(running=False, error=f"Something went wrong: {e}", step="")
        finally:
            self.progress["running"] = False

    async def _query(self, c, q: str, rows: int = 500) -> list[dict]:
        r = await c.get("https://archive.org/advancedsearch.php", params={
            "q": q, "fl[]": ["identifier", "title", "year", "date", "downloads", "item_size", "description"],
            "rows": rows, "output": "json", "sort[]": "downloads desc"})
        r.raise_for_status()
        return r.json()["response"]["docs"]

    async def _search(self, c, key, holiday: str | None = None) -> list[dict]:
        label, _m, _n, _ch, words = PROFILES[key]
        self._step(f"Searching the Internet Archive for {label}{' ' + holiday if holiday else ''}…")
        extra = {"halloween": " AND (halloween OR spooky OR \"trick or treat\" OR october)",
                 "christmas": " AND (christmas OR xmas OR holiday OR santa OR december)"}.get(holiday, "")
        queries = [
            f"title:({words}) AND title:(commercial* OR promo* OR bumper* OR bumps OR ads OR idents OR break* OR "
            f"\"coming up next\" OR \"you're watching\")"
            f"{extra} AND mediatype:movies",
            # whole broadcasts with their commercials; only the breaks are kept
            f"title:({words}) AND (title:(woc OR airing OR \"with commercials\" OR \"original commercials\" OR "
            f"\"full broadcast\") OR description:(\"with commercials\" OR \"commercials included\" OR "
            f"\"original commercials\")){extra} AND mediatype:movies",
            # tapes whose description, not title, says what they are
            f"description:({words}) AND title:(commercial* OR break*){extra} AND mediatype:movies",
        ]
        seen, docs = set(), []
        for q in queries:
            try:
                for d in await self._query(c, q):
                    if d["identifier"] not in seen:
                        seen.add(d["identifier"])
                        docs.append(d)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("Archive search failed (%s): %s", q[:60], e)
        return docs

    async def _candidates(self, c, scored: list[tuple[float, dict, str]], have: set, limit: int = 40) -> list[dict]:
        """Read the file lists of the best-matching tapes (several at once) and rank them by the real
        quality of their files, so the sharpest tapes are downloaded first."""
        sem = asyncio.Semaphore(6)

        async def one(score, doc, kind):
            async with sem:
                try:
                    meta = (await c.get(f"https://archive.org/metadata/{doc['identifier']}")).json()
                except (httpx.HTTPError, ValueError):
                    return None
            files = _pick_files(meta.get("files", []), kind)
            if not files:
                return None
            qs = [file_quality(f) for f in files]
            best = max(qs, key=quality_score)
            return {"doc": doc, "kind": kind, "files": files, "q": best, "rel": score,
                    "desc": (meta.get("metadata") or {}).get("description", ""),
                    "date": str((meta.get("metadata") or {}).get("date") or ""),
                    "rank": score + quality_score(best) / 10}

        todo = [(s, d, k) for s, d, k in scored if d["identifier"] not in have][:limit]
        self._step(f"Checking the picture quality of {len(todo)} tapes…")
        got = [x for x in await asyncio.gather(*(one(*t) for t in todo)) if x]
        if self.min_height:
            got = [x for x in got if not x["q"]["h"] or x["q"]["h"] >= self.min_height]
        got.sort(key=lambda x: -x["rank"])
        return got

    async def _network(self, c, key, era, docs, budget, holiday: str | None = None, airings: bool = True):
        label = PROFILES[key][0]
        y0, y1 = ERAS[era]
        scored = []
        for d in docs:
            sc, _why, kind = judge(key, d, y0, y1)
            if sc > 0 and (airings or kind == "ads"):
                scored.append((sc, d, kind))
        scored.sort(key=lambda x: -x[0])
        if holiday:  # a holiday run only wants tapes that are clearly that holiday's
            scored = [x for x in scored if holiday_of(_title(x[1])) == holiday
                      or (holiday == "halloween" and month_of(_title(x[1])) == "october")
                      or (holiday == "christmas" and month_of(_title(x[1])) == "december")]
        idx = self.index()
        have = {t["id"] for t in idx.get(key, [])}
        used = sum(t.get("bytes", 0) for t in idx.get(key, [])
                   if t.get("era") == era and bool(t.get("holiday")) == bool(holiday))
        what = f"{label} {holiday + ' ' if holiday else ''}{era}"
        self.progress["found"].append({"network": what, "matches": len(scored)})
        if used >= budget:
            return
        for cand in await self._candidates(c, scored, have):
            if self.stop or used >= budget:
                break
            title = _title(cand["doc"]) or cand["doc"]["identifier"]
            # folder names become tags: "nick", "90s", and "halloween" (holiday ads, only in season)
            # or "october" (an October tape: favoured in October, played all year)
            season = holiday_of(title) or month_of(title)
            folder = OWN / key / era / season if season else OWN / key / era
            used += await self._take(c, cand, key, folder, label, budget - used, era=era)

    async def _bumpers(self, c, key, docs, budget):
        label = PROFILES[key][0]
        scored = [(sc, d, k) for d in docs for sc, _w, k in [judge(key, d, 0, 0, undated_bumpers=True)] if k == "bumps"]
        scored.sort(key=lambda x: -x[0])
        idx = self.index()
        have = {t["id"] for t in idx.get(key, [])}
        used = sum(t.get("bytes", 0) for t in idx.get(key, []) if t.get("kind") == "bumps")
        self.progress["found"].append({"network": f"{label} bumpers", "matches": len(scored)})
        for cand in await self._candidates(c, scored, have, limit=15):
            if self.stop or used >= budget:
                break
            cand["kind"] = "ads"  # downloaded and split like any commercials tape
            used += await self._take(c, cand, key, OWN / key / "bumpers", label, budget - used,
                                     extra={"kind": "bumps"})

    # ---- one tape -------------------------------------------------------------------------
    async def _take(self, c, cand: dict, key: str, folder, label: str, room: int, era: str = "",
                    extra: dict | None = None) -> int:
        """Fetch one tape, find its commercials, and file it. Returns the bytes kept."""
        doc, kind = cand["doc"], cand["kind"]
        title = _title(doc) or doc["identifier"]
        size = sum(int(f.get("size") or 0) for f in cand["files"])
        if kind == "ads" and size > room * 1.15:
            return 0
        if shutil.disk_usage(DATA_DIR).free - (size if kind == "ads" else 2e9) < KEEP_FREE:
            self.progress["error"] = "Stopped: the disk is nearly full."
            self.stop = True
            return 0
        names = ad_names(cand.get("desc")) if kind == "ads" else []
        tape = {"id": doc["identifier"], "title": title, "era": era or era_tag(min(tape_years(doc) or {0})) or "",
                "kind": kind, "files": [], "clips": 0, "dupes": 0, "bytes": 0, "years": sorted(tape_years(doc)),
                "holiday": holiday_of(title) or "", "month": month_of(title) or "", "q": None, **(extra or {})}
        kept = []
        for f in cand["files"]:
            if self.stop:
                break
            url = f"https://archive.org/download/{doc['identifier']}/{f['name']}"
            base = f"{doc['identifier'][:60]}--{os.path.splitext(os.path.basename(f['name']))[0][:60]}"
            if kind == "airing":
                kept += await self._take_airing(cand, f, url, folder, base, title, tape)
            else:
                dest = folder / f"{doc['identifier'][:60]}--{os.path.basename(f['name'])}"
                self._step(f"{label}: downloading “{title[:60]}”", done=0, total=int(f.get("size") or 0))
                await self._download(c, url, dest)
                got = await self._file_clips(dest, title, cand, names if len(cand["files"]) == 1 else [], tape)
                if got:
                    kept.append(dest)
                else:
                    dest.unlink(missing_ok=True)
        if not tape["clips"]:
            for d in kept:
                d.unlink(missing_ok=True)
                (d.parent / (d.name + ".clips.json")).unlink(missing_ok=True)
            self._note(f"Skipped “{title[:70]}”: no clear commercials in it")
            return 0
        tape["files"] = [d.name for d in kept]
        tape["bytes"] = sum(d.stat().st_size for d in kept)
        idx = self.index()
        idx.setdefault(key, []).append(tape)
        self._save_index(idx)
        had = f", {tape['dupes']} you already had" if tape["dupes"] else ""
        self._note(f"Added {tape['clips']} commercials from “{title[:70]}” ({quality_label(tape['q'])} picture{had})")
        return tape["bytes"]

    async def _file_clips(self, dest, title: str, cand: dict, names: list[str], tape: dict,
                          airing: dict | None = None) -> bool:
        """Find the commercials in a downloaded file, check its picture and write its clips list."""
        ff = self.app.ffmpeg.ffmpeg
        self._step(f"Finding the commercials in “{title[:60]}”", done=0, total=0)
        clips = await asyncio.to_thread(find_clips_ex, ff, str(dest))
        if not clips:
            return False
        q = await asyncio.to_thread(probe_video, ff, str(dest))
        mid = clips[len(clips) // 2]
        q["eh"] = await asyncio.to_thread(effective_height, ff, str(dest), q["h"], mid["s"], min(20.0, mid["d"]))
        if self.min_height and q["eh"] and q["eh"] < self.min_height:
            self._note(f"Skipped “{title[:60]}”: only {q['eh']} lines of real picture")
            return False
        q["score"] = quality_score(q)
        if names and len(names) == len(clips):  # the uploader listed the ads in order
            for clip, name in zip(clips, names):
                clip["n"] = name
        dupes = await self._dedupe(str(dest), clips, q["score"], title)
        data = {"v": 2, "source": f"https://archive.org/details/{cand['doc']['identifier']}", "title": title,
                "q": q, "clips": clips}
        if airing:
            data["airing"] = airing
        (dest.parent / (dest.name + ".clips.json")).write_text(json.dumps(data))
        tape["clips"] += len(clips) - dupes
        tape["dupes"] += dupes
        if not tape["q"] or q["score"] > quality_score(tape["q"]):
            tape["q"] = q
        return True

    async def _take_airing(self, cand, f, url, folder, base, title, tape) -> list:
        """A broadcast with its commercials: read it over the network, keep only the breaks."""
        ff = self.app.ffmpeg.ffmpeg
        mins = int(file_quality(f)["len"] // 60)
        self._step(f"Looking for the breaks in “{title[:60]}” ({mins} min, read straight from the archive)",
                   done=0, total=0)
        found = await asyncio.to_thread(find_breaks, ff, url)
        if not found:
            self._note(f"No commercial breaks found in “{title[:60]}”")
            return []
        kept, record = [], {"id": cand["doc"]["identifier"], "title": title, "text": _norm(
            title + " " + re.sub(r"<[^>]+>", " ", _text(cand.get("desc")))[:3000]),
            "date": cand.get("date", ""), "years": sorted(tape_years(cand["doc"])), "file": f["name"], "breaks": []}
        folder.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(found):
            if self.stop:
                break
            dest = folder / f"{base}--break{i + 1:02d}.mkv"
            self._step(f"Saving break {i + 1} of {len(found)} from “{title[:50]}”")
            if not await asyncio.to_thread(extract, ff, url, b["s"], b["e"], str(dest)):
                dest.unlink(missing_ok=True)
                continue
            if await self._file_clips(dest, title, cand, [], tape, airing={"at": b["at"], "n": i + 1}):
                kept.append(dest)
                record["breaks"].append({"at": b["at"], "file": str(dest)})
            else:
                dest.unlink(missing_ok=True)
        if record["breaks"]:
            rec = [a for a in self.airings() if not (a["id"] == record["id"] and a["file"] == record["file"])]
            self._save_json(AIRINGS, rec + [record])
        return kept

    async def _dedupe(self, path: str, clips: list[dict], score: float, title: str) -> int:
        """Mark commercials Rerun already has a copy of. When the new copy looks better, the old one
        comes off the air instead. Returns how many of the new ones are duplicates."""
        ff = self.app.ffmpeg.ffmpeg
        try:
            prints = json.loads(PRINTS.read_text())
        except (OSError, ValueError):
            prints = {}
        dupes, retire = 0, {}
        for i, clip in enumerate(clips):
            h = await asyncio.to_thread(fingerprint, ff, path, clip["s"], clip["d"])
            me = {"d": clip["d"], "h": h, "q": score}
            twin = next((k for k, p in prints.items() if not p.get("dup") and same_ad(me, p)), None) if h else None
            if twin and prints[twin]["q"] >= score - 5:
                clip["dup"] = True
                dupes += 1
            elif twin:
                prints[twin]["dup"] = True
                retire.setdefault(prints[twin]["file"], []).append(prints[twin]["i"])
            prints[f"{path}#{i}"] = {**me, "file": path, "i": i, "dup": bool(clip.get("dup"))}
        for file, ids in retire.items():
            self._mark_dup(file, ids)
        self._save_json(PRINTS, prints)
        return dupes

    def _mark_dup(self, file: str, ids: list[int]):
        p = file + ".clips.json"
        try:
            data = json.loads(open(p, encoding="utf-8").read())
        except (OSError, ValueError):
            return
        for i in ids:
            if i < len(data.get("clips", [])) and isinstance(data["clips"][i], dict):
                data["clips"][i]["dup"] = True
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

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
            if ch.get("plan"):
                continue  # the 25 Days channel picks its own commercials
            names = {ch["name"].lower(), ch.get("auto_key", "")[8:].lower()}
            for key in keys:
                if idx.get(key) and any(n.lower() in names for n in PROFILES.get(key, ("", "", "", []))[3]):
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
        try:
            prints = json.loads(PRINTS.read_text())
            gone = str(OWN / key)
            self._save_json(PRINTS, {k: p for k, p in prints.items() if not p["file"].startswith(gone)})
            self._save_json(AIRINGS, [a for a in self.airings()
                                      if not any(b["file"].startswith(gone) for b in a["breaks"])])
        except (OSError, ValueError):
            pass
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

    # ---- ABC Family's 25 Days of Christmas --------------------------------------------------
    def start_25days(self, year: int, budget: int, min_height: int = 0, nearby: bool = True):
        self.min_height = int(min_height or 0)
        self._begin(self._run_25days, int(year), budget, nearby)

    async def _run_25days(self, year: int, budget: int, nearby: bool):
        """The real commercials from ABC Family's 25 Days of Christmas: tapes from that December first,
        then the years either side (the same ads and 25 Days promos often ran several years)."""
        from .xmasdata import YEAR_HINTS
        try:
            self._register_folder()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": UA}) as c:
                self._step("Searching the Internet Archive for ABC Family's 25 Days of Christmas…")
                queries = [
                    '"25 days of christmas" AND mediatype:movies',
                    '(title:("abc family") OR description:("abc family")) AND (christmas OR december OR holiday '
                    'OR "25 days") AND mediatype:movies',
                    'title:("abc family") AND title:(commercial* OR break* OR promo* OR bumper*) AND mediatype:movies',
                ]
                seen, docs = set(), []
                for q in queries:
                    try:
                        for d in await self._query(c, q, 1000):
                            if d["identifier"] not in seen:
                                seen.add(d["identifier"])
                                docs.append(d)
                    except (httpx.HTTPError, ValueError, KeyError) as e:
                        log.warning("Archive search failed: %s", e)
                hints = YEAR_HINTS.get(year, [])
                rounds = [[year]] + ([[year - 1, year + 1], [year - 2, year + 2]] if nearby else [])
                idx = self.index()
                have = {t["id"] for t in idx.get("25days", [])}
                used = sum(t.get("bytes", 0) for t in idx.get("25days", []) if year in t.get("for", []))
                for years in rounds:
                    scored = []
                    for d in docs:
                        sc, kind, yr = judge_25days(d, years, hints)
                        if sc > 0:
                            scored.append((sc, d, kind))
                    scored.sort(key=lambda x: -x[0])
                    label = f"25 Days of Christmas {'/'.join(map(str, years))}"
                    self.progress["found"].append({"network": label, "matches": len(scored)})
                    for cand in await self._candidates(c, scored, have):
                        if self.stop or used >= budget:
                            break
                        ty = sorted(y for y in tape_years(cand["doc"]) if y in years) or [years[0]]
                        folder = OWN / "25days" / f"y{ty[0]}"
                        got = await self._take(c, cand, "25days", folder, "25 Days", budget - used,
                                               era=era_tag(ty[0]) or "", extra={"for": [year], "year": ty[0]})
                        used += got
                        have.add(cand["doc"]["identifier"])
                    if self.stop or used >= budget:
                        break
            await self._rescan()
            self._step("Done. Your library is being rescanned so the 25 Days channel picks them up.",
                       running=False)
        except Exception as e:
            log.exception("25 Days finder failed")
            self.progress.update(running=False, error=f"Something went wrong: {e}", step="")
        finally:
            self.progress["running"] = False

    def summary_25days(self) -> dict:
        idx = self.index()
        tapes = idx.get("25days", [])
        by_year: dict = {}
        for t in tapes:
            y = str(t.get("year", ""))
            by_year.setdefault(y, {"tapes": 0, "clips": 0, "airings": 0})
            by_year[y]["tapes"] += 1
            by_year[y]["clips"] += t.get("clips", 0)
            by_year[y]["airings"] += 1 if t.get("kind") == "airing" else 0
        return {"tapes": [{"title": t["title"], "year": t.get("year"), "kind": t.get("kind", "ads"),
                           "clips": t.get("clips", 0), "quality": quality_label(t.get("q"))} for t in tapes],
                "by_year": by_year, "status": self.progress,
                "airings": [{"title": a["title"], "breaks": len(a["breaks"]), "date": a.get("date", "")}
                            for a in self.airings() if "/25days/" in a["breaks"][0]["file"].replace("\\", "/")]}


def judge_25days(doc: dict, years: list[int], hints: list[str]) -> tuple[float, str, int]:
    """Is this tape from ABC Family's 25 Days of Christmas (or ABC Family in December) in one of these
    years? Returns (score, "ads"/"airing", the year)."""
    title = _title(doc)
    desc = _text(doc.get("description"))
    text = f"{title} {desc[:1500]}"
    if not re.search(r"abc family|25 days of christmas", text, re.I):
        return 0, "", 0
    if re.search(r"freeform|fox family|family channel|fox kids", title, re.I) and not re.search(r"abc family", title,
                                                                                                re.I):
        return 0, "", 0
    if _FOREIGN.search(title) or _NEVER.search(title) or _NEVER.search(desc[:2000]):
        return 0, "", 0
    yrs = tape_years(doc)
    yr = next((y for y in years if y in yrs), 0)
    if not yr:
        return 0, "", 0
    december = bool(re.search(r"christmas|december|holiday|xmas|santa|25 days|\b12[/.-]\d", text, re.I))
    if not december:
        return 0, "", 0
    if _AIRING.search(title) or re.search(r"commercials (are )?included|with (original )?commercials", desc, re.I):
        kind = "airing"
    elif _ADS.search(title) or re.search(r"commercial", desc[:300], re.I):
        kind = "ads"
    else:
        return 0, "", 0
    score = 3 + math.log10(1 + int(doc.get("downloads") or 0))
    score += 4 if re.search(r"25 days of christmas", text, re.I) else 0
    score += 3 if any(h in text.lower() for h in hints) else 0
    score += 2 if _BETTER.search(title) else 0
    score -= 3 if _WORSE.search(title) else 0
    return score, kind, yr


def _pick_files(files: list[dict], kind: str = "ads") -> list[dict]:
    """The video files to use from an item: the file as uploaded (best quality; the archive's own MP4
    copy is re-compressed and softer), unless it's huge, then the archive's copy. Airing tapes are read
    over the network, so any size is fine for them."""
    limit = MAX_AIRING if kind == "airing" else MAX_ITEM
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
        best = f if 0 < int(f.get("size") or 0) <= limit or not derived else \
            min(derived, key=lambda d: int(d.get("size") or 0))
        if 0 < int(best.get("size") or 0) <= limit:
            out.append(best)
    return out
