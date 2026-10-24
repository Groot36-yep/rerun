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
import logging
import os
import secrets
import tempfile
import time

log = logging.getLogger("rerun.stream")

TS_PACKET = 188
PMT_PID, VIDEO_PID = 0x1000, 0x100   # fixed by joiner_cmd
GOP_LIMIT = 8 * 1024 * 1024
TARGET_LEAD = 5.0      # seconds the stream tries to stay ahead of the clock (a cushion against hiccups)
FIRST_BURST = 10.0     # fill the player's buffer quickly when a channel starts, so Plex starts sooner
QUICK_MIN = 2.0        # without ffmpeg's burst option, top up with quick pieces at least this long
IDLE_GRACE = 20        # keep a channel warm briefly so flipping back is instant
BAD_FILE_MINUTES = 30
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
        burst = FIRST_BURST if self.segments == 0 else max(0.0, min(FIRST_BURST, TARGET_LEAD - lead))
        self.segments += 1
        if ch is None:
            return {"item": {"k": "x", "t": ""}, "offset": 0, "dur": 30, "burst": burst, "end": self.cursor + 30}
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
        return {"item": item, "offset": offset, "dur": dur, "burst": burst, "end": end, "block": block}


class StreamManager:
    def __init__(self, app, port: int):
        self.app = app
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
        if item.get("k") in ("c", "f"):
            src = self.app.media_input(item)
            if src and self._is_bad(src):
                src = None
            elif not src:
                log.warning("Can't find %s (%s); showing a card instead", item.get("t") or "a file", item.get("f"))
        has_audio = bool(item.get("a", True))
        if src:
            cmd, kind = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio), "media"
        else:
            text = st.name if item.get("k") == "x" and not item.get("t") else f"{st.name}\n\nWe'll be right back"
            if item.get("k") == "x" and item.get("t"):
                text = f"{st.name}\n\n{item['t']}"
            cmd, kind = self._slate(st, text, dur, burst), "slate"

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
                try:
                    first = await asyncio.wait_for(proc.stdout.read(65536), 30)
                except asyncio.TimeoutError:
                    first = b""
                    await _kill(proc)
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
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, False, hw_decode)
                    continue
                if kind == "media" and hw_decode and self.ffmpeg.encoder_status.get("using") not in (None, "none"):
                    hw_decode = False  # some files only decode on the processor; try that before giving up
                    cmd = self.ffmpeg.media_cmd(src, item.get("in", 0) + plan["offset"], dur, s, burst, has_audio, False)
                    continue
                if kind == "media":
                    log.warning("Couldn't play %s: %s", item.get("t") or src, msg[-300:] or "unknown error")
                    self.bad[src] = time.time() + BAD_FILE_MINUTES * 60
                    cmd, kind = self._slate(st, f"{st.name}\n\nWe'll be right back", dur, burst), "slate"
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
