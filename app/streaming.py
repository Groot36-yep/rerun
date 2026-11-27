"""Live streams.

How a channel plays:
  * One "joiner" ffmpeg per channel reads an endless playlist of pieces from
    Rerun itself and glues them into a single MPEG-TS stream, fixing the
    timestamps at every seam (this is the approach dizqueTV proved with Plex).
  * Each piece is one schedule item (part of an episode, a commercial...) that
    a short-lived ffmpeg encodes at real-time speed into a fixed format.
  * Everyone watching the same channel shares one joiner, so the channel is
    only encoded once.
  * If a file is missing or broken, that piece becomes a "We'll be right back"
    card instead of ending the stream.
"""
from __future__ import annotations

import asyncio
import random
import logging
import os
import re
import secrets
import tempfile
import time

from . import archive, images, looks, onair, seasons, trivia
from .scheduler import airs_now, in_season
from .channels import filler_pool, network_key, network_logo
from .ffmpeg import RESOLUTIONS
from .lineups import LINEUPS

log = logging.getLogger("rerun.stream")

TS_PACKET = 188
PMT_PID, VIDEO_PID = 0x1000, 0x100   # fixed by joiner_cmd
GOP_LIMIT = 8 * 1024 * 1024
TARGET_LEAD = 8.0      # seconds the stream tries to stay ahead of the clock (a cushion against hiccups)
FIRST_BURST = 10.0     # fill the player's buffer quickly when a channel starts, so Plex starts sooner
# Every piece starts with at least this much made faster than real time. "Even out the volume"
# (loudnorm) listens about 3 s ahead before it lets any sound out, so a piece started at play speed
# gave nothing for ~2.6 s; with a break's run of short commercials the cushion wore down to ~2 s
# and a hiccup froze the picture. With the head start a piece's first picture comes in ~0.2 s.
MIN_BURST = 3.0
DRAIN_ABOVE = 4.0      # more than this past the target: no head start, let the cushion settle back
SPLIT_BEFORE = 2.0     # a piece with a costly overlay later on stops this long before it (see _split_for_overlay)


def head_start(segments: int, lead: float, has_burst: bool) -> float:
    """Seconds of the next piece to make faster than real time, given how far ahead of the clock the
    channel is (`lead`) and how many pieces it has played."""
    if segments == 0:
        return FIRST_BURST
    if not has_burst:  # older ffmpeg: head starts are separate quick pieces (see plan)
        return max(0.0, min(FIRST_BURST, TARGET_LEAD - lead))
    if lead >= TARGET_LEAD + DRAIN_ABOVE:
        return 0.0
    # cover the piece's own start-up, plus whatever the cushion is short of the target
    return min(FIRST_BURST, MIN_BURST + max(0.0, TARGET_LEAD - lead))
QUICK_MIN = 2.0        # without ffmpeg's burst option, top up with quick pieces at least this long
IDLE_GRACE = 20        # keep a channel warm briefly so flipping back is instant
BAD_FILE_MINUTES = 10
LIST_ENTRIES = 20000


class TunersBusy(Exception):
    pass


def _oneline(buf) -> str:
    text = buf.decode(errors="replace") if isinstance(buf, (bytes, bytearray)) else str(buf)
    return " ".join(text.split())


async def _drain(reader: asyncio.StreamReader, buf: bytearray, limit: int = 16384):
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > limit:
                del buf[: len(buf) - limit]
    except Exception:
        return


# What ffmpeg says when the graphics chip can't decode a video (AV1 on older Intel chips, say). It
# doesn't stop after that: it keeps running and makes nothing.
_CHIP_REFUSED = re.compile(r"Function not implemented|Failed setup for format vaapi|hwaccel initiali[sz]ation returned error|"
                           r"No support for codec \S+ profile|Failed to get HW surface format", re.I)
FIRST_DATA_LIMIT = 30.0


async def _first_data(proc, err: bytearray, chip: bool, limit: float = FIRST_DATA_LIMIT) -> bytes:
    """A piece's first output, or b"" (and ffmpeg stopped) if none comes within `limit` seconds, or, when
    the chip decodes, as soon as ffmpeg says the chip refused the video: waiting out the limit then froze
    the channel for 30 seconds a try."""
    read = asyncio.ensure_future(proc.stdout.read(65536))
    deadline = time.monotonic() + limit
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            done, _ = await asyncio.wait({read}, timeout=min(0.2, left))
            if done:
                return read.result()
            if chip and _CHIP_REFUSED.search(bytes(err[-4000:]).decode("utf-8", "replace")):
                break
    finally:
        if not read.done():
            read.cancel()
    await _kill(proc)
    return b""


async def _kill(proc):
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass


def _pes_pts(data: bytes, off: int) -> int | None:
    """The presentation time (90 kHz ticks) at the start of a video frame, if this packet has one."""
    afc = (data[off + 3] >> 4) & 3
    i = off + 4
    if afc & 2:
        i += 1 + data[off + 4]
    if not afc & 1 or i + 14 > off + TS_PACKET or data[i:i + 3] != b"\x00\x00\x01" or not data[i + 7] & 0x80:
        return None
    p = data[i + 9:i + 14]
    return ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1


class Subscriber:
    def __init__(self, who: str = ""):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.dead = False
        self.who = who
        self.since = time.time()

    def feed(self, chunk: bytes):
        if self.dead:
            return
        try:
            self.q.put_nowait(chunk)
        except asyncio.QueueFull:
            log.warning("A viewer fell too far behind and was disconnected")
            self.dead = True
            self.end()

    def end(self):
        while True:
            try:
                self.q.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    self.q.get_nowait()
                except asyncio.QueueEmpty:
                    pass


class ChannelStream:
    def __init__(self, mgr: "StreamManager", ch: dict):
        self.mgr = mgr
        self.cid = ch["id"]
        self.name = ch["name"]
        self.number = ch["number"]
        self.token = secrets.token_urlsafe(18)
        self.subs: set[Subscriber] = set()
        self.proc = None
        self.seg_proc = None
        self.cursor: float | None = None
        self.segments = 0
        self.ts_offset = 0.0       # where timestamps continue from if the joiner restarts
        self.joiner_media = 0.0    # seconds of video the current joiner has been given
        self.bytes = 0
        self.started = time.time()
        self.idle_since: float | None = None
        self.stopping = False
        self.done = False
        self.restarts: list[float] = []
        self.now_playing = ""
        self.pat = self.pmt = b""
        self.gop: list[bytes] = []
        self.gop_bytes = 0
        # How far ahead of the clock the picture is, read from the stream's own timestamps.
        self.pts_first: int | None = None
        self.pts_top = 0
        self.wall_first = 0.0
        self.text_file = os.path.join(tempfile.gettempdir(), f"rerun-slate-{self.token}.txt")
        self._task: asyncio.Task | None = None

    async def start(self):
        await self._spawn_joiner()
        self._task = asyncio.create_task(self._pump())

    async def _spawn_joiner(self):
        url = f"http://127.0.0.1:{self.mgr.port}/internal/list/{self.token}"
        if self.joiner_media:
            self.ts_offset += self.joiner_media + 2.0
            self.joiner_media = 0.0
        cmd = self.mgr.ffmpeg.joiner_cmd(url, f"{self.number} {self.name}", self.ts_offset)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self.errors = bytearray()
        self._err_task = asyncio.create_task(_drain(self.proc.stderr, self.errors))
        log.info("Channel %s %s started", self.number, self.name)

    def _remember_keyframe(self, data: bytes):
        """Track the latest PAT/PMT and everything since the last video keyframe, so a new
        viewer can start with a clean picture instead of waiting for the next keyframe."""
        last_key = -1
        for off in range(0, len(data), TS_PACKET):
            b1 = data[off + 1]
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            if pid == 0:
                self.pat = data[off:off + TS_PACKET]
            elif pid == PMT_PID:
                self.pmt = data[off:off + TS_PACKET]
            elif pid == VIDEO_PID and b1 & 0x40:
                if (data[off + 3] >> 4) & 2 and data[off + 4] and data[off + 5] & 0x40:
                    last_key = off
                pts = _pes_pts(data, off)
                if pts is not None:
                    if self.pts_first is None:
                        self.pts_first, self.wall_first = pts, time.time()
                    elif pts < self.pts_first:
                        pts += 1 << 33  # the 26-hour timestamp wraparound
                    self.pts_top = max(self.pts_top, pts)
        if last_key >= 0:
            self.gop, self.gop_bytes = [data[last_key:]], len(data) - last_key
        elif self.gop:
            self.gop.append(data)
            self.gop_bytes += len(data)
            if self.gop_bytes > GOP_LIMIT:
                self.gop, self.gop_bytes = [], 0

    async def _pump(self):
        try:
            while True:
                rest = b""
                while True:
                    chunk = await self.proc.stdout.read(65536)
                    if not chunk:
                        break
                    data = rest + chunk
                    cut = len(data) - len(data) % TS_PACKET  # only ever hand out whole packets
                    data, rest = data[:cut], data[cut:]
                    if not data:
                        continue
                    self.bytes += len(data)
                    self._remember_keyframe(data)
                    for sub in list(self.subs):
                        sub.feed(data)
                        if sub.dead:
                            self.unsubscribe(sub)
                await self.proc.wait()
                if self.stopping or not self.subs:
                    break
                now = time.time()
                self.restarts = [t for t in self.restarts if t > now - 600] + [now]
                log.warning("Channel %s stream stopped unexpectedly (%s); restarting",
                            self.number, _oneline(self.errors)[-300:] or "no details")
                if len(self.restarts) > 5:
                    log.error("Channel %s keeps failing; giving up for now", self.number)
                    break
                await asyncio.sleep(1)
                await self._spawn_joiner()
        except Exception:
            log.exception("Channel %s stream error", self.number)
        finally:
            self.done = True
            for sub in list(self.subs):
                sub.end()
            self.subs.clear()
            await _kill(self.proc)
            await _kill(self.seg_proc)
            try:
                os.unlink(self.text_file)
            except OSError:
                pass
            self.mgr.forget(self)
            log.info("Channel %s %s stopped", self.number, self.name)

    def ahead(self) -> float | None:
        """Seconds of picture made beyond what real time has used up since the channel started."""
        if self.pts_first is None:
            return None
        return (self.pts_top - self.pts_first) / 90000 - (time.time() - self.wall_first)

    def subscribe(self, who: str = "") -> Subscriber:
        sub = Subscriber(who)
        if self.gop and self.pat and self.pmt:
            sub.feed(self.pat + self.pmt + b"".join(self.gop))
        self.subs.add(sub)
        self.idle_since = None
        return sub

    def unsubscribe(self, sub: Subscriber):
        self.subs.discard(sub)
        if not self.subs and self.idle_since is None:
            self.idle_since = time.time()

    async def stop(self):
        self.stopping = True
        await _kill(self.seg_proc)
        await _kill(self.proc)
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    # ---- pieces -----------------------------------------------------------------------
    async def plan(self) -> dict:
        app = self.mgr.app
        ch = app.channel(self.cid)
        now = time.time()
        if self.cursor is None:
            self.cursor = now
        lead = self.cursor - now
        if lead < -8 or lead > 90:
            log.info("Channel %s re-synced with the schedule (was %+.0fs off)", self.number, lead)
            self.cursor, lead = now, 0.0
        burst = head_start(self.segments, lead, self.mgr.ffmpeg.has_burst)
        self.segments += 1
        if ch is None:
            return {"item": {"k": "x", "t": ""}, "offset": 0, "dur": 30, "burst": burst, "end": self.cursor + 30}
        if (ch.get("quick") or {}).get("until") and now > ch["quick"]["until"]:
            asyncio.ensure_future(app.schedules.end_test_if_over(ch))  # test mode is over: back to normal
        p = app.schedules.get(ch["id"])
        if p.end < self.cursor + 900 or p.sig != app.schedules.sig(ch):
            p = await app.schedules.ensure(ch, until=self.cursor + 3 * 3600)
        idx = p.index_at(self.cursor)
        if idx is not None and p.items[idx]["e"] - self.cursor < 0.5 and idx + 1 < len(p.items):
            self.cursor = p.items[idx]["e"]
            idx += 1
        if idx is None:
            return {"item": {"k": "x", "t": ""}, "offset": 0, "dur": 10, "burst": burst, "end": self.cursor + 10}
        item = p.items[idx]
        offset = self.cursor - item["s"]
        block = p.block_at(self.cursor) or {}
        self.now_playing = block.get("title") or item.get("t") or ""
        dur, end = item["e"] - self.cursor, item["e"]
        if not self.mgr.ffmpeg.has_burst:
            # Older ffmpeg (4.4 on Ubuntu 22.04) can't start a piece fast and then slow to play speed,
            # so the head start is made as its own short piece, encoded flat out, and the rest of
            # the item follows at play speed. The joiner stitches them together seamlessly.
            if burst >= QUICK_MIN:
                dur = min(dur, burst)
                end, burst = self.cursor + dur, None
            else:
                burst = 0.0
        overlay = await self._overlay(ch, p, idx, item, block, end) if item.get("k") == "c" else None
        if overlay:
            dur, end, overlay = self._split_for_overlay(overlay, dur, end)
        return {"item": item, "offset": offset, "dur": dur, "burst": burst, "end": end, "block": block,
                "overlay": overlay}

    def _split_for_overlay(self, ov: dict, dur: float, end: float) -> tuple[float, float, dict | None]:
        """An animation (Nick 2009) or a green-screen template is costly: every frame of the piece it's in
        is run through its layers, even long before it shows. On a 24-minute episode that's 24 minutes of
        extra work, which a 4-core server already busy with Plex couldn't keep up with (the picture fell
        behind and froze). So the piece stops just before it, and the next piece starts there and carries
        it for its few seconds."""
        starts = [float(a["t0"]) for a in ov.get("anims") or []]
        if ov.get("template"):
            starts.append(float(ov["template"]["t0"]))
        first = min(starts, default=0.0)
        if first <= SPLIT_BEFORE + 2:
            return dur, end, ov
        cut = first - SPLIT_BEFORE
        for b in sorted(ov.get("banners") or [], key=lambda b: -b["t0"]):  # never cut a banner off mid-way
            if b["t0"] < cut < b["t1"] + 0.5:
                cut = b["t0"] - SPLIT_BEFORE
        if cut <= SPLIT_BEFORE:
            return dur, end, ov
        ov = {k: v for k, v in ov.items() if k not in ("anims", "template")}
        banners = [b for b in ov.get("banners") or [] if b["t1"] <= cut]
        if banners:
            ov["banners"] = banners
        else:
            ov.pop("banners", None)
        return cut, end - (dur - cut), ov or None

    async def _overlay(self, ch: dict, p, idx: int, item: dict, block: dict, end: float) -> dict | None:
        """What to draw over a show: its network's logo in the corner (the one it had the year the episode
        aired), "Up next" near the end of the last part before the closing break, "After the break" going
        into a mid-show break, and a "Tonight at 8" promo early in the block (see onair.py)."""
        app = self.mgr.app
        s = app.cfg.data["stream"]
        out: dict = {}
        H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])[1]
        net = self._bug_network(ch, item)
        tag = archive.tag_for_network(net) if net else None
        if s.get("screen_bug", True) and net:
            year = self._year(item)
            try:
                ref = images.era_logo(tag, year) or network_logo(net, app.catalog)
                logo = await images.screen_bug(ref, H, app.cfg.data["metadata"].get("online_lookups", True),
                                               [net, tag or ""], year)
            except Exception as e:
                log.info("No on-screen logo (%s)", e.__class__.__name__)
                logo = None
            if logo:
                out["logo"] = logo
        rel_end = end - self.cursor - 1
        banners, anims = [], []
        chosen_tpl, chosen_style = looks.upnext_override(app.cfg.data, tag)  # the On-air look page's pick
        for g in onair.plan(p, idx, item, block, s, block.get("title", "")):
            t0, t1 = max(0.5, g["t0"] - self.cursor), min(g["t1"] - self.cursor, rel_end)
            if t1 - t0 < (6 if g["kind"] == "up_next" else 4):
                continue  # this piece only has a sliver of it (a late start or a quick-start piece)
            if g["kind"] == "up_next":
                nxt = g["block"]
                # a template named after the next show first, then the network's pick, then one named after it
                tpl_file = images.find_template([nxt["title"]]) or chosen_tpl or (
                    None if chosen_style is not None or images.theme_for(tag) in images.ANIMATED
                    else images.find_template([net, tag or ""] if net else []))  # Nick: its recreation, not a template
                tpl = await asyncio.to_thread(images.analyse_template, tpl_file, self.mgr.ffmpeg.ffmpeg) \
                    if tpl_file else None
                if tpl:  # your own template: it plays for its own length (pictures for 10 seconds)
                    a = max(0.5, item["e"] - 32 - self.cursor)
                    b = min(a + (tpl["dur"] or 10.0), rel_end)
                    if b - a >= 3:
                        out["template"] = {**tpl, "file": tpl_file, "t0": a, "t1": b}
                        titled = images.template_title(nxt["title"], tpl_file, H)
                        if titled:
                            out["template"].update(title_png=titled[0], title_x=titled[1]["x"],
                                                   title_y=titled[1]["y"])
                    continue
            try:  # in the network's 2000s style, or in the colour of its logo
                theme = chosen_style if chosen_style is not None else images.theme_for(tag, plan=bool(ch.get("plan")))
                about = g["block"] or {}
                # a show with several cut-outs: a different one from one airing to the next
                pick = int((about.get("s") or item.get("s") or 0) // 60)
                n9 = looks.n9_options(app.cfg.data)
                want_art = theme not in images.ANIMATED or n9["poster"]
                art = await images.artwork(app, about["img"]) \
                    if about.get("img") and theme in images.ART_THEMES and want_art and g["kind"] != "teaser" else None
                if theme in images.ANIMATED:  # Nick 2009: an animation over the whole picture, its own length
                    a = max(0.5, item["e"] - 32 - self.cursor) if g["kind"] == "up_next" else t0
                    length = min(images.N9["len"], rel_end - a)
                    if length >= 5:
                        layers = await asyncio.to_thread(
                            images.nick2009_layers, g["title"], g["label"], H,
                            images.character_for(g["title"], pick) if g["kind"] != "teaser" else None, art,
                            n9["clear"], n9["poster"])
                        anims.append({**layers, "t0": a, "len": length})
                    continue
                banner = await asyncio.to_thread(images.up_next_banner, g["title"], H,
                                                 images.bug_accent(out.get("logo")), theme, art,
                                                 about.get("img") or "", out.get("logo"), g["label"], g["short"],
                                                 pick)
                if banner:
                    banners.append({"file": banner, "art": images.banner_art(banner), "t0": t0, "t1": t1,
                                    "motion": images.MOTION.get(theme, "left"), "place": images.PLACE.get(theme, "")})
            except Exception as e:
                log.info("No %s banner (%s)", g["kind"], e.__class__.__name__)
        if banners:
            out["banners"] = banners
        if anims:
            out["anims"] = anims
        return out or None

    async def ident(self, item: dict, caption: str = "") -> str | None:
        """The station ID picture for this moment: the logo of the network of the show around it (the
        one it had the year that episode aired), as on the corner logo. None if there's no logo to use."""
        app = self.mgr.app
        ch = app.channel(self.cid)
        if not ch:
            return None
        p = app.schedules.get(self.cid)
        i = p.index_at(item.get("s", 0) + 0.01)
        show = item if item.get("k") == "c" else None
        j = i if i is not None else len(p.items) - 1
        while show is None and j is not None and j >= 0:  # the show this break belongs to
            if p.items[j].get("k") == "c":
                show = p.items[j]
            j -= 1
        net = self._bug_network(ch, show or {})
        online = app.cfg.data["metadata"].get("online_lookups", True)
        raw = None
        if net:
            tag = archive.tag_for_network(net)
            year = self._year(show or {})
            own = images.custom_bug([net, tag or ""], year)
            if own:
                raw = open(own, "rb").read()
            elif online:
                ref = images.era_logo(tag, year) or network_logo(net, app.catalog)
                raw = await images.official_logo(ref) if ref else None
        if not raw:  # no network logo (a movie channel, a holiday channel): the channel's own, as in Plex
            raw = await images.channel_logo(ch, app.catalog, app.festive(), online)
        if not raw:
            return None
        H = RESOLUTIONS.get(app.cfg.data["stream"].get("resolution"), RESOLUTIONS["720p"])[1]
        return await asyncio.to_thread(images.station_id, raw, H, caption)

    def _year(self, item: dict):
        """The year an episode first aired (or a movie came out), for the logo of that era."""
        cat = self.mgr.app.catalog
        m = cat.items.get(item.get("m") or "") or {}
        return (m.get("date") or "")[:4] or m.get("year") or (cat.shows.get(m.get("show"), {}) or {}).get("year")

    def _bug_network(self, ch: dict, item: dict) -> str:
        """Whose logo goes in the corner: the channel's own on a network's channel (Nickelodeon, Boomerang,
        the 25 Days channel), otherwise the network the show first aired on."""
        app = self.mgr.app
        auto = network_key(ch)
        if auto.startswith("lineup:"):
            return (LINEUPS.get(auto[7:]) or {}).get("network", "")
        if (ch.get("plan") or {}).get("name") == "abcfamily25":
            return "ABC Family"
        if auto.startswith("network:"):
            return auto[len("network:"):]
        m = app.catalog.items.get(item.get("m") or "") or {}
        return ((app.catalog.shows.get(m.get("show"), {}) or {}).get("network") if m.get("show")
                else m.get("network")) or ""


def _bad_key(src: str, item: dict) -> str:
    """What a failure counts against: a commercial on a tape is just that commercial (its neighbours on
    the same tape may play fine); a show is its whole file."""
    return f"{src}#{item.get('in', 0)}" if item.get("k") == "f" else src


class StreamManager:
    def __init__(self, app, port: int):
        self.app = app
        self._pools: dict[str, tuple] = {}  # each channel's commercials, for stand-ins
        self.port = port
        self.ffmpeg = app.ffmpeg
        self.by_channel: dict[str, ChannelStream] = {}
        self.by_token: dict[str, ChannelStream] = {}
        self.bad: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, ch: dict, who: str = "") -> tuple[Subscriber, ChannelStream]:
        async with self._lock:
            st = self.by_channel.get(ch["id"])
            if st is None or st.done or st.stopping:
                tuners = int(self.app.cfg.data["stream"].get("tuners") or 4)
                if len([s for s in self.by_channel.values() if not s.done]) >= tuners:
                    raise TunersBusy()
                st = ChannelStream(self, ch)
                self.by_channel[ch["id"]] = st
                self.by_token[st.token] = st
                await st.start()
            return st.subscribe(who), st

    def forget(self, st: ChannelStream):
        if self.by_channel.get(st.cid) is st:
            self.by_channel.pop(st.cid, None)
        self.by_token.pop(st.token, None)

    async def reaper(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            self.bad = {k: v for k, v in self.bad.items() if v > now}
            for st in list(self.by_channel.values()):
                if not st.subs and st.idle_since and now - st.idle_since > IDLE_GRACE:
                    await st.stop()

    async def stop_channel(self, cid: str):
        st = self.by_channel.get(cid)
        if st:
            await st.stop()

    async def stop_all(self):
        for st in list(self.by_channel.values()):
            await st.stop()

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {"channel": st.cid, "number": st.number, "name": st.name, "viewers": len(st.subs),
             "uptime": int(now - st.started), "now_playing": st.now_playing,
             "ahead_seconds": round(st.ahead(), 1) if st.ahead() is not None else None,
             "watchers": [s.who for s in st.subs]}
            for st in self.by_channel.values() if not st.done
        ]

    def playlist(self, token: str) -> str | None:
        if token not in self.by_token:
            return None
        base = f"http://127.0.0.1:{self.port}/internal/piece/{token}"
        return "ffconcat version 1.0\n" + "".join(f"file '{base}/{n}'\n" for n in range(LIST_ENTRIES))

    def _is_bad(self, path: str) -> bool:
        exp = self.bad.get(path)
        return bool(exp and exp > time.time())

    async def _trivia_cmd(self, st, item: dict, dur: float, burst, s: dict) -> list[str] | None:
        """The trivia card for this piece, in the network's style or over its chosen background."""
        ch = self.app.channel(st.cid)
        if not ch:
            return None
        net = st._bug_network(ch, item)
        tag = archive.tag_for_network(net) if net else None
        look = (self.app.cfg.data.get("looks") or {}).get(tag or "", {})
        bg = trivia.background([net, tag or ""], look.get("trivia_bg", ""))
        theme = images.theme_for(tag, plan=bool(ch.get("plan")))
        W, H = RESOLUTIONS.get(s.get("resolution"), RESOLUTIONS["720p"])
        png = await trivia.make(self.app, item, theme, W, H, bg)
        if not png:
            return None
        bg_audio = await asyncio.to_thread(trivia.has_audio, bg, self.ffmpeg.ffprobe) if bg else False
        return self.ffmpeg.still_cmd(png, dur, s, burst, bg, bg_audio)

    def _stand_in(self, cid: str, item: dict, need: float):
        """Another commercial (or bumper, for a bumper) from the channel's own, at least `need` seconds
        long and as close to it as can be, whose file is there and plays. (item, source) or None."""
        ch = self.app.channel(cid)
        if not ch:
            return None
        cat = self.app.catalog
        stamp = (id(cat), cat.scanned_at, repr(ch.get("commercials")))  # made again after a scan or an edit
        cached = self._pools.get(ch["id"])
        if not cached or cached[0] != stamp:
            cached = (stamp, filler_pool(ch, cat))
            self._pools[ch["id"]] = cached
        was = self.app.catalog.items.get(item.get("m") or "") or {}
        bumper = "bumper" in (was.get("tags") or [])
        listener = getattr(self.app, "listener", None)
        heard = listener.heard if listener else None
        active = seasons.active(self.app.cfg.data.get("holidays") or seasons.DEFAULT_HOLIDAYS, time.time())
        cands = [f for f in cached[1] if ("bumper" in (f.get("tags") or [])) == bumper and f["id"] != item.get("m")
                 and f["dur"] >= need - 0.3 and not f.get("dup") and in_season(f, heard, active)
                 and airs_now(f, heard, time.time())]
        random.shuffle(cands)
        cands.sort(key=lambda f: round(f["dur"] - need))  # the closest length first; equal ones shuffled
        for f in cands[:12]:
            alt = {"s": item.get("s", 0), "e": item.get("e", 0), "k": "f", "m": f["id"], "in": f.get("in", 0),
                   "t": f.get("title", ""), "f": f.get("file") or "", "p": f.get("part") or "", "a": bool(f.get("audio", True))}
            src = self.app.media_input(alt)
            if src and not self._is_bad(_bad_key(src, alt)):
                log.info("Playing %s in place of %s", alt["t"] or "another commercial", item.get("t") or "a missing one")
                return alt, src
        return None

    def _slate(self, st: ChannelStream, text: str, dur: float, burst: float) -> list[str]:
        try:
            with open(st.text_file, "w", encoding="utf-8") as f:
                f.write(text)
            tf = st.text_file
        except OSError:
            tf = None
        return self.ffmpeg.slate_cmd(tf, dur, self.app.cfg.data["stream"], burst)

    async def piece(self, token: str):
        """Async generator producing one encoded piece of the channel."""
        st = self.by_token.get(token)
        if st is None or st.stopping:
            return
        lead = (st.cursor - time.time()) if st.cursor is not None else 0.0
        plan = await st.plan()
        item, dur, burst = plan["item"], max(0.5, plan["dur"]), plan["burst"]
        spinup, delivered = 0.0, False
        s = self.app.cfg.data["stream"]
        src = None
        ov = plan.get("overlay")
        swapped = False
        if item.get("k") in ("c", "f"):
            src = self.app.media_input(item)
            if src and self._is_bad(_bad_key(src, item)):
                src = None
            elif not src:
                log.warning("Can't find %s (%s)", item.get("t") or "a file", item.get("f"))
            if not src and item.get("k") == "f":
                # A commercial or bumper that's gone missing (or won't play): another one of the same
                # length takes its place, rather than a "We'll be right back" card.
                stand = self._stand_in(st.cid, item, plan["offset"] + dur)
                if stand:
                    item, src, swapped = stand[0], stand[1], True
        if src:  # what kind of video it is (looked up once, off the main thread): see FFmpeg.chip_decodes
            await asyncio.to_thread(self.ffmpeg.codec_of, src)
        card_cmd = None
        if item.get("k") == "q":  # "Guess the show!"
            try:
                card_cmd = await self._trivia_cmd(st, item, dur, burst, s) if s.get("trivia", True) else None
            except Exception as e:
                log.warning("Couldn't make the trivia card (%s)", e)
            if not card_cmd:  # trivia switched off, or no picture to show: a commercial instead
                stand = self._stand_in(st.cid, {**item, "k": "f", "m": ""}, plan["offset"] + dur)
                if stand:
                    item, src, swapped = stand[0], stand[1], True
        has_audio = bool(item.get("a", True))
        if card_cmd:
            cmd, kind = card_cmd, "card"
        elif src:
            cmd, kind = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio,
                                              overlay=ov, gpu=True), "media"
        else:
            text = (st.name if (item.get("k") == "x" and not item.get("t")) or item.get("k") == "q"
                    else f"{st.name}\n\nWe'll be right back")
            if item.get("k") == "x" and item.get("t"):
                text = f"{st.name}\n\n{item['t']}"
            cmd, kind = None, "slate"
            if not (item.get("k") == "x" and item.get("t")):  # not a card with something to say: a station ID
                try:
                    png = await st.ident(item, "" if text == st.name else "We'll be right back")
                    cmd = self.ffmpeg.ident_cmd(png, dur, s, burst) if png else None
                except Exception as e:
                    log.info("No station ID (%s)", e.__class__.__name__)
            if cmd is None:
                cmd = self._slate(st, text, dur, burst)

        proc = None
        try:
            hw_decode = True
            for _attempt in range(5):
                spawned = time.time()
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                st.seg_proc = proc
                err = bytearray()
                err_task = asyncio.create_task(_drain(proc.stderr, err))
                first = await _first_data(proc, err, chip=kind == "media" and "-hwaccel" in cmd)
                if first:
                    spinup, delivered = time.time() - spawned, True
                    self.app.diag.piece(st.number, kind, spinup, lead, burst, dur, True)
                    yield first
                    while True:
                        chunk = await proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                    await proc.wait()
                    await err_task
                    if proc.returncode and kind == "media" and not st.stopping:
                        log.warning("Problem partway through %s: %s", item.get("t") or "a file",
                                    _oneline(err)[-300:] or "no details")
                    break
                await proc.wait()
                await err_task
                if st.stopping:
                    break
                msg = _oneline(err)
                if kind == "media" and has_audio and "matches no streams" in msg:
                    has_audio = False
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, False, hw_decode,
                                                overlay=ov, gpu=True)
                    continue
                if kind == "media" and hw_decode and "-hwaccel" in cmd and _CHIP_REFUSED.search(msg):
                    # a kind of video the chip can't decode at all (AV1 on older Intel chips): straight to
                    # the processor, and every file of that kind from now on
                    codec = self.ffmpeg.chip_refused(src, msg)
                    log.info("Decoding %s on the processor (the graphics chip can't decode %s)",
                             item.get("t") or src, codec or "it")
                    hw_decode = False
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio, False,
                                                overlay=ov, gpu=True)
                    continue
                if kind == "media" and "-hwaccel_output_format" in cmd:
                    # the chip couldn't take this file all the way (a kind it can't decode, most likely):
                    # its pictures go through the processor, decoded on the chip where it can
                    log.info("Sizing %s on the processor (%s)", item.get("t") or src, msg[-160:] or "no details")
                    self.ffmpeg.cpu_frames_for(src)
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio,
                                                hw_decode, overlay=ov, gpu=True)
                    continue
                if kind == "media" and hw_decode and self.ffmpeg.encoder_status.get("using") not in (None, "none"):
                    hw_decode = False  # some files only decode on the processor; try that before giving up
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio, False,
                                                overlay=ov, gpu=True)
                    continue
                if kind == "card":
                    log.warning("Couldn't show the trivia card (%s)", msg[-200:] or "unknown error")
                    stand = self._stand_in(st.cid, {**item, "k": "f", "m": ""}, plan["offset"] + dur)
                    if stand:
                        item, src, swapped = stand[0], stand[1], True
                        has_audio = bool(item.get("a", True))
                        cmd, kind = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst,
                                                          has_audio, hw_decode), "media"
                    else:
                        cmd, kind = self._slate(st, st.name, dur, burst), "slate"
                    continue
                if kind == "media" and ov:
                    # the logo or a banner is what failed, most likely: the show plays without them
                    log.warning("Couldn't draw over %s (%s); playing it without the logo and banners",
                                item.get("t") or src, msg[-200:] or "unknown error")
                    ov = None
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio,
                                                hw_decode, overlay=None, gpu=True)
                    continue
                if kind == "media":
                    log.warning("Couldn't play %s: %s", item.get("t") or src, msg[-300:] or "unknown error")
                    self.bad[_bad_key(src, item)] = time.time() + BAD_FILE_MINUTES * 60
                    stand = self._stand_in(st.cid, item, plan["offset"] + dur) if item.get("k") == "f" and not swapped else None
                    if stand:
                        item, src, swapped = stand[0], stand[1], True
                        has_audio = bool(item.get("a", True))
                        cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio,
                                                    hw_decode)
                        continue
                    text = st.name if item.get("k") == "f" else f"{st.name}\n\nWe'll be right back"
                    cmd, kind = self._slate(st, text, dur, burst), "slate"
                    continue
                if kind == "slate":
                    log.warning("Couldn't draw the placeholder card (%s); using a plain screen", msg[-200:])
                    cmd, kind = self.ffmpeg.blank_cmd(dur, s), "blank"
                    continue
                log.error("Couldn't make even a plain screen: %s", msg[-300:])
                break
            st.cursor = plan["end"]
            st.joiner_media += dur
            if not st.stopping and not delivered:
                self.app.diag.piece(st.number, kind, spinup, lead, burst, dur, False)
        finally:
            await _kill(proc)
            if st.seg_proc is proc:
                st.seg_proc = None
