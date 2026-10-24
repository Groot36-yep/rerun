"""Read plain folders of videos (commercials, bumpers, music videos) without Plex."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.folders")

VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts",
    ".webm", ".flv", ".vob", ".3gp", ".divx", ".ogv",
}

_JUNK = re.compile(
    r"\s*[\(\[](official( music)?( video| audio| hd video)?|music video|hd|hq|720p|1080p|4k|remastered|lyrics?)[\)\]]",
    re.I,
)
_MV = re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)(?:\s*[\(\[](?P<year>(?:19|20)\d\d)[\)\]])?$")
_DECADE = re.compile(r"^(?:(19|20)?(\d)0)'?s$")


def file_id(path: str) -> str:
    return "f" + hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def clean_title(stem: str) -> str:
    t = _JUNK.sub("", stem.replace("_", " ")).strip()
    return re.sub(r"\s+", " ", t) or stem


def parse_music_video(stem: str, folder_tags: list[str]) -> dict:
    t = clean_title(stem)
    out = {"artist": "", "title": t, "year": 0}
    m = _MV.match(t)
    if m:
        out.update(artist=m["artist"].strip(), title=m["title"].strip(), year=int(m["year"] or 0))
    if not out["year"]:
        for tag in folder_tags:
            if re.fullmatch(r"(19|20)\d\d", tag):
                out["year"] = int(tag)
            else:
                d = _DECADE.match(tag)
                if d:
                    century = d.group(1) or ("20" if d.group(2) in "012" else "19")
                    out["year"] = int(f"{century}{d.group(2)}5")
    if not out["artist"] and folder_tags:
        out["artist"] = folder_tags[-1].title()
    return out


def _clips(path: str) -> dict | None:
    """The list of commercials in a recorded break, if Rerun found them (see archive.py)."""
    try:
        with open(path + ".clips.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_videos(root: str) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@")))
        for fn in sorted(filenames):
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() in VIDEO_EXT:
                files.append(os.path.join(dirpath, fn))
    return files


class Prober:
    """ffprobe with a cache keyed on file size and modification time."""

    def __init__(self, ffprobe: str):
        self.ffprobe = ffprobe
        self.path = DATA_DIR / "probe_cache.json"
        self.cache: dict = read_json(self.path, {})
        self.sem = asyncio.Semaphore(4)

    async def probe(self, path: str) -> dict | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        hit = self.cache.get(path)
        if hit and hit["m"] == int(st.st_mtime) and hit["s"] == st.st_size and "q" in hit["r"]:
            return hit["r"]
        async with self.sem:
            data = await self._run(path, ["-show_format", "-show_streams", "-show_chapters"])
        if data is None:
            return None
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not (s.get("disposition") or {}).get("attached_pic")), {})
        fmt = data.get("format") or {}
        try:
            num, den = (video.get("avg_frame_rate") or "0/1").split("/")
            fps = round(int(num) / int(den), 2) if int(den) else 0
        except ValueError:
            fps = 0
        r = {
            "h": int(video.get("height") or 0),
            # exact picture quality, shown on the Commercials page
            "q": {"w": int(video.get("width") or 0), "h": int(video.get("height") or 0), "fps": fps,
                  "kbps": int(int(fmt.get("bit_rate") or 0) / 1000), "codec": video.get("codec_name", "")},
            "dur": round(float((data.get("format") or {}).get("duration") or 0), 3),
            "v": any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                     for s in streams),
            "a": any(s.get("codec_type") == "audio" for s in streams),
            "ch": [round(float(c.get("start_time") or 0), 3) for c in data.get("chapters") or []],
        }
        self.cache[path] = {"m": int(st.st_mtime), "s": st.st_size, "r": r}
        return r

    async def chapters(self, path: str) -> list[float]:
        r = await self.probe(path)
        return r["ch"] if r else []

    async def _run(self, path: str, what: list[str]):
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffprobe, "-v", "error", "-print_format", "json", *what, path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            log.error("Could not run ffprobe: %s", e)
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("ffprobe timed out on %s", path)
            return None
        try:
            return json.loads(out or b"{}")
        except ValueError:
            return None

    def save(self):
        atomic_write_json(self.path, self.cache)


LONG_AD = 150  # a "commercial" file longer than this is a compilation, to be split into single ads


def _cached_clips(path: str, size: int, mtime: int) -> list | None:
    try:
        with open(DATA_DIR / "clips" / (file_id(path) + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        return d["clips"] if d.get("s") == size and d.get("m") == mtime else None
    except (OSError, ValueError, KeyError):
        return None


async def split_long(path: str, splitter) -> list:
    """Where the single commercials sit in a long compilation file; worked out once, then remembered."""
    st = os.stat(path)
    clips = _cached_clips(path, st.st_size, int(st.st_mtime))
    if clips is None:
        clips = await asyncio.to_thread(splitter, path)
        (DATA_DIR / "clips").mkdir(parents=True, exist_ok=True)
        atomic_write_json(DATA_DIR / "clips" / (file_id(path) + ".json"),
                          {"s": st.st_size, "m": int(st.st_mtime), "clips": clips})
        log.info("Split %s into %d commercials", os.path.basename(path), len(clips))
    return clips


async def scan_folder(root: str, kind: str, prober: Prober, progress=None, splitter=None) -> list[dict]:
    """kind is 'music_videos' or 'commercials'. Sub-folder names become tags. With a splitter, long
    commercial compilations are split into single commercials."""
    if not os.path.isdir(root):
        log.warning("Folder %s is missing or not mounted", root)
        return []
    files = await asyncio.to_thread(list_videos, root)
    done = 0

    async def one(p):
        nonlocal done
        r = await prober.probe(p)
        done += 1
        if progress:
            progress(done, len(files))
        return r

    results = await asyncio.gather(*(one(p) for p in files))
    src = f"folder:{root}"
    items = []
    for p, r in zip(files, results):
        if not r or not r["v"] or r["dur"] < 1:
            continue
        rel = Path(os.path.relpath(p, root))
        folder_tags = [t.lower() for t in rel.parts[:-1]]
        stem = rel.stem
        clips = _clips(p)
        if clips is None and kind == "commercials" and r["dur"] > LONG_AD and splitter:
            if progress:
                progress(0, 0, f"Finding the commercials in {os.path.basename(p)}")
            found = await split_long(p, splitter)
            if not found:
                log.info("Left out %s: it's %d minutes long and has no clear breaks between ads",
                         os.path.basename(p), r["dur"] // 60)
                continue
            clips = {"title": stem, "clips": found}
        if clips is not None and kind == "commercials":
            # A recorded commercial break: each commercial plays straight out of the tape.
            base = clean_title(clips.get("title") or stem)
            for i, (start, length) in enumerate(clips.get("clips") or []):
                if start + length <= r["dur"] + 1:
                    items.append({"id": f"{file_id(p)}c{i}", "dur": length, "in": start, "file": p, "part": "",
                                  "audio": r["a"], "chapters": [], "tags": folder_tags, "src": src,
                                  "title": f"{base} ({i + 1})", "h": r.get("h", 0), "q": r.get("q")})
            continue
        if kind == "commercials" and r["dur"] > LONG_AD * 4:
            continue  # a long recording with no splitter: it could never fit in a break
        item = {"id": file_id(p), "dur": r["dur"], "file": p, "part": "", "audio": r["a"],
                "chapters": r["ch"], "tags": folder_tags, "src": src, "h": r.get("h", 0), "q": r.get("q")}
        if kind == "music_videos":
            item.update(parse_music_video(stem, folder_tags))
        else:
            item["title"] = clean_title(stem)
        items.append(item)
    return items
