"""See what a channel will show, without waiting for it on live TV.

`rundown()` lists what's coming up, item by item: the show, the network's ident before it, the bumper
into each break, the commercials, and where the "Up next" banner and the logo appear.
`render()` makes a short MP4 of any stretch of a channel's schedule, drawn by exactly the same code as
live TV (the same pieces, logo, banner and templates), so it can be watched in the browser.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time

from .store import DATA_DIR

log = logging.getLogger("rerun.preview")

PREVIEW_DIR = DATA_DIR / "previews"
MAX_LENGTH = 180  # seconds
_busy: asyncio.Lock | None = None  # one preview at a time: they're made flat out on the processor


def _is_bumper(app, item: dict) -> bool:
    f = app.catalog.items.get(item.get("m") or "") or {}
    return "bumper" in (f.get("tags") or [])


async def rundown(app, ch: dict, start: float, hours: float = 3.0) -> list[dict]:
    """What plays from `start` on: each item with what it is, plus where the breaks are."""
    hours = max(0.25, min(float(hours), 12.0))
    end = start + hours * 3600
    p = await app.schedules.ensure(ch, until=end + 600)
    s = app.cfg.data["stream"]
    items = [x for x in p.items if x["e"] > start and x["s"] < end]
    out = []
    for i, x in enumerate(items):
        block = p.block_at(x["s"]) or {}
        kind = {"c": "show", "f": "commercial", "x": "card"}.get(x["k"], "card")
        nxt = items[i + 1] if i + 1 < len(items) else None
        if kind == "commercial" and _is_bumper(app, x):
            # an ident sits right before a show starts; a bumper leads into a break
            kind = "ident" if nxt and nxt["k"] == "c" and (i == 0 or items[i - 1]["k"] != "c") else "bumper"
        row = {"s": x["s"], "e": x["e"], "kind": kind, "title": x.get("t") or "", "block": block.get("title", "")}
        if kind == "show":
            later = any(y["k"] == "c" for y in items[i + 1:] if y["s"] < block.get("e", 0))
            nb = p.block_at(block.get("e", 0) + 1) if block else None
            row["logo"] = bool(s.get("screen_bug", True))
            if s.get("up_next", True) and not later and nb and nb.get("title") and (nb.get("cats") or nb.get("img")):
                row["up_next"] = nb["title"]
        if kind in ("bumper", "commercial", "card") and (not out or out[-1]["kind"] in ("show",)):
            row["break_starts"] = True  # a good place to preview: the end of the show, then the break
        out.append(row)
    return out


async def render(app, ch: dict, start: float, length: float = 75.0) -> str:
    """An MP4 of `length` seconds of the channel from `start`, as live TV would show it."""
    global _busy
    if _busy is None:
        _busy = asyncio.Lock()
    length = max(5.0, min(float(length), MAX_LENGTH))
    sig = hashlib.sha1(app.schedules.sig(ch).encode()).hexdigest()[:8]  # a rebuilt schedule makes a new one
    final = PREVIEW_DIR / f"{ch['id']}-{int(start)}-{int(length)}-{sig}.mp4"
    async with _busy:
        if final.is_file() and final.stat().st_size > 0:
            return str(final)  # made already
        return await _render(app, ch, start, length, final)


async def _render(app, ch: dict, start: float, length: float, final) -> str:
    from .streaming import ChannelStream
    p = await app.schedules.ensure(ch, until=start + length + 600)
    s = dict(app.cfg.data["stream"])
    s["video_kbps"] = min(int(s.get("video_kbps") or 4000), 2500)  # lighter: it's a preview
    ff = app.ffmpeg
    work = tempfile.mkdtemp(prefix="rerun-preview-")
    # The live stream's own code decides the logo, the banner and the templates; a stand-in stream
    # whose clock is set to each piece's start lets the preview use it unchanged.
    stand_in = ChannelStream.__new__(ChannelStream)
    stand_in.mgr = app.streams
    pieces, t, n = [], start, 0
    text_file = os.path.join(work, "card.txt")
    try:
        while t < start + length - 0.3:
            idx = p.index_at(t)
            if idx is None:
                break
            item = p.items[idx]
            end = min(item["e"], start + length)
            dur = end - t
            if dur < 0.2:
                t = end + 0.01
                continue
            block = p.block_at(t) or {}
            stand_in.cursor = t
            overlay = await stand_in._overlay(ch, p, idx, item, block, end) if item.get("k") == "c" else None
            src = app.media_input(item) if item.get("k") in ("c", "f") else None
            out = os.path.join(work, f"{n:04d}.ts")
            if src:
                cmd = ff.media_cmd(src, item.get("in", 0) + (t - item["s"]), dur, s, None, bool(item.get("a", True)),
                                   overlay=overlay)
            else:
                with open(text_file, "w", encoding="utf-8") as fh:
                    fh.write(f"{ch['name']}\n\n{item.get('t') or 'We will be right back'}")
                cmd = ff.slate_cmd(text_file, dur, s, None)
            res = await asyncio.to_thread(_run_to, cmd, out)
            if res != 0 and src:  # e.g. a file without sound: try once more with made-up silence
                cmd = ff.media_cmd(src, item.get("in", 0) + (t - item["s"]), dur, s, None, False, overlay=overlay)
                res = await asyncio.to_thread(_run_to, cmd, out)
            if res == 0 and os.path.getsize(out) > 0:
                pieces.append(out)
            n += 1
            t = end
        if not pieces:
            raise RuntimeError("Nothing to show at that time")
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        for old in PREVIEW_DIR.glob("*.mp4"):  # keep just the last few
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)
        listing = os.path.join(work, "list.txt")
        with open(listing, "w", encoding="utf-8") as fh:
            fh.writelines(f"file '{x}'\n" for x in pieces)
        res = await asyncio.to_thread(subprocess.run, [ff.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                                                        "-safe", "0", "-i", listing, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                                                        "-movflags", "+faststart", str(final)], capture_output=True,
                                    timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace")[-200:] or "Couldn't put the preview together")
        return str(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_to(cmd: list[str], out: str) -> int:
    try:
        with open(out, "wb") as fh:
            return subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL, timeout=240).returncode
    except subprocess.TimeoutExpired:
        return 1
