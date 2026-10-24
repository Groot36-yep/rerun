"""A plain-text report to paste when asking for help: what Plex asked for, how fast channels
started, how hard the machine worked, and recent problems. Nothing secret goes in it."""
from __future__ import annotations

import asyncio
import os
import platform
import re
import time
from collections import Counter, deque

from . import __version__, images

# Requests worth recording: everything Plex and players fetch. Rerun's own page and its
# internal piece requests are left out.
_SKIP = ("/static/", "/api/", "/internal/", "/health", "/netlogo/", "/holidaylogo/")
_SECRET = re.compile(r"(token|key|password)=[^&\s]+", re.I)


def _clean(text: str) -> str:
    return _SECRET.sub(lambda m: m.group(1) + "=…", text)


def _proc_times() -> tuple[dict, float]:
    """CPU ticks used so far by every program, and by the whole processor."""
    procs = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
        except OSError:
            continue
        name = raw[raw.index("(") + 1:raw.rindex(")")]
        fields = raw[raw.rindex(")") + 2:].split()
        procs[pid] = (name, int(fields[11]) + int(fields[12]))
    with open("/proc/stat") as f:
        cpu = [int(x) for x in f.readline().split()[1:]]
    return procs, cpu


async def _cpu_use():
    """Overall busy %, and the busiest programs over the next second (Linux only)."""
    if not os.path.exists("/proc/stat"):
        return None
    a, ca = _proc_times()
    await asyncio.sleep(1.0)
    b, cb = _proc_times()
    tick = os.sysconf("SC_CLK_TCK")
    total, idle = sum(cb) - sum(ca), (cb[3] + cb[4]) - (ca[3] + ca[4])
    busy = 100 * (total - idle) / total if total else 0
    use = Counter()
    count = Counter()
    for pid, (name, t) in b.items():
        if pid in a:
            use[name] += t - a[pid][1]
            count[name] += 1
    procs = [(n, 100 * t / tick, count[n]) for n, t in use.most_common() if t > 0]
    return busy, procs


class Diagnostics:
    def __init__(self):
        self.requests: deque = deque(maxlen=400)
        self.pieces: deque = deque(maxlen=400)
        self.tunes: deque = deque(maxlen=100)
        self.started = time.time()
        self.video_seconds = 0.0

    # ---- recording ------------------------------------------------------------------------
    @staticmethod
    def wants(path: str) -> bool:
        return path != "/" and not path.startswith(_SKIP)

    def request(self, ip: str, method: str, path: str, status: int, ms: float, agent: str):
        self.requests.append((time.time(), ip, method, path, status, ms, agent[:60]))

    def piece(self, number: int, kind: str, spinup: float, lead: float, burst: float, seconds: float, ok: bool):
        self.pieces.append((time.time(), number, kind, spinup, lead, burst, seconds, ok))
        if ok:
            self.video_seconds += seconds

    def tune(self, number: int, first_bytes: float, already_on: bool, agent: str):
        self.tunes.append((time.time(), number, first_bytes, already_on, agent[:60]))

    # ---- the report ----------------------------------------------------------------------
    async def report(self, app) -> str:
        cpu_now = await _cpu_use()  # takes a second: it watches the processor for a moment
        now = time.time()
        out = [f"Rerun {__version__} diagnostic report — {time.strftime('%Y-%m-%d %H:%M:%S')}",
               f"Up for {_dur(now - app.started)}"]

        out += ["", "== Computer =="] + _machine()
        enc = app.ffmpeg.encoder_status
        out.append(f"Video encoding: {enc.get('label') or enc.get('using')} (setting: {enc.get('requested')})"
                   + (f", device {enc['device']}" if enc.get("device") else "")
                   + (f" — {enc['message']}" if enc.get("message") else ""))
        out.append(f"ffmpeg: {app.ffmpeg.version[:90]}")
        if not app.ffmpeg.has_burst:
            out.append("  (older ffmpeg without quick start: Rerun makes each head start as its own quick piece)")
        s = app.cfg.data["stream"]
        out.append(f"Picture: {s.get('resolution')} at {s.get('video_kbps')} kbps, even volume "
                   f"{'on' if s.get('even_volume', True) else 'off'}, {s.get('tuners')} tuners")
        if cpu_now:
            busy, procs = cpu_now
            out.append(f"Processor right now: {busy:.0f}% busy overall. Busiest programs (100% = one core):")
            for name, pct, count in procs[:8]:
                out.append(f"  {pct:5.0f}%  {name}" + (f" ×{count}" if count > 1 else ""))

        out += ["", "== Channels =="]
        chans = sorted(app.cfg.data["channels"], key=lambda c: c["number"])
        on = [c for c in chans if app.channel_visible(c)]
        out.append(f"{len(chans)} channels, {len(on)} in the guide. Festive logos "
                   f"{'on' if app.cfg.data.get('festive_logos') else 'off'}.")
        logos = images.LOGO_STATUS
        for c in chans[:60]:
            ls = logos.get(c["id"], "")
            out.append(f"  {c['number']:>4} {c['name'][:30]:<30} {'on ' if app.channel_visible(c) else 'off'}"
                       f"  logo: {ls or 'not made yet'}")

        out += ["", "== What Plex and players asked for (most recent last) =="]
        reqs = list(self.requests)
        if not reqs:
            out.append("Nothing yet. If Plex is set up, it hasn't contacted Rerun since it started.")
        else:
            kinds = Counter(_kind(r[3]) for r in reqs)
            out.append("Totals: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
            bad = [r for r in reqs if r[4] >= 400]
            if bad:
                out.append(f"Refused or failed: {len(bad)}")
            ips = Counter(r[1] for r in reqs)
            out.append("From: " + ", ".join(f"{ip} ({n})" for ip, n in ips.most_common(5)))
            logo = [r for r in reqs if _kind(r[3]) == "logos"]
            if logo:
                out.append(f"Logos: {len(logo)} asked for, {sum(1 for r in logo if r[4] < 400)} sent; slowest "
                           f"{max(r[5] for r in logo):.0f}ms; last at {time.strftime('%H:%M:%S', time.localtime(logo[-1][0]))} "
                           f"by {logo[-1][6] or 'unknown'}")
            out.append("(Plex checks in every few seconds; those check-ins are counted above but not listed.)")
            quiet = [r for r in reqs if _kind(r[3]) not in ("tuner info",) and not r[3].startswith("/lineup_status")]
            for t, ip, method, path, status, ms, agent in quiet[-30:]:
                out.append(f"  {time.strftime('%H:%M:%S', time.localtime(t))} {ip:<15} {method:<4} {status} "
                           f"{ms:>6.0f}ms {_clean(path)[:70]}  [{agent}]")

        out += ["", "== Tuning in (most recent last) =="]
        if not self.tunes:
            out.append("No one has tuned in since Rerun started.")
        for t, number, first, already, agent in list(self.tunes)[-15:]:
            out.append(f"  {time.strftime('%H:%M:%S', time.localtime(t))} channel {number}: first picture data "
                       f"after {first:.2f}s{' (channel was already on)' if already else ''}  [{agent}]")

        out += ["", "== Stream pieces (most recent last) =="]
        pcs = list(self.pieces)
        if pcs:
            media = [p for p in pcs if p[2] == "media"]
            spin = sorted(p[3] for p in media) or [0]
            low = [p for p in pcs if p[4] < 1.0 and p[5] == 0]
            out.append(f"{len(pcs)} pieces: {len(media)} from files, {sum(1 for p in pcs if p[2] != 'media')} cards, "
                       f"{sum(1 for p in pcs if not p[7])} failed. First data: typical {spin[len(spin) // 2]:.2f}s, "
                       f"slowest {spin[-1]:.2f}s. Close calls (less than 1s of picture to spare): {len(low)}")
            for t, number, kind, spinup, lead, burst, secs, ok in pcs[-25:]:
                how = "quick start" if burst is None else f"head start {burst:.0f}s" if burst else "play speed"
                out.append(f"  {time.strftime('%H:%M:%S', time.localtime(t))} ch {number:<4} {kind:<6} "
                           f"first data {spinup:.2f}s, {lead:+.1f}s ahead, {secs:.0f}s long, {how}"
                           f"{'' if ok else '  FAILED'}")
        else:
            out.append("None yet.")
        live = app.streams.status()
        for x in live:
            ahead = "" if x["ahead_seconds"] is None else f", picture {x['ahead_seconds']:+.1f}s ahead of real time"
            out.append(f"Playing now: ch {x['number']} for {_dur(x['uptime'])}{ahead}; "
                       f"watched by {', '.join(x['watchers']) or 'no one (closing soon)'}")

        out += ["", "== Recent warnings and errors =="]
        probs = [l for l in app.logs.lines if l["level"] in ("warning", "error", "critical")][-30:]
        if not probs:
            out.append("None.")
        for l in probs:
            out.append(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(l['t']))} {l['level'].upper():<7} "
                       f"{_clean(l['msg'])[:220]}")
        return "\n".join(out) + "\n"


def _kind(path: str) -> str:
    for prefix, name in (("/logo/", "logos"), ("/stream/", "streams"), ("/auto/", "streams"), ("/xmltv", "guide"),
                         ("/lineup", "channel list"), ("/discover", "tuner info"), ("/device", "tuner info"),
                         ("/img/", "artwork"), ("/iptv", "playlist")):
        if path.startswith(prefix):
            return name
    return "other"


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 \
        else f"{s // 60}m {s % 60}s"


def _machine() -> list[str]:
    lines = [f"{platform.system()} {platform.release()}, Python {platform.python_version()}"]
    model = ""
    try:
        with open("/proc/cpuinfo") as f:
            model = next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), "")
    except OSError:
        model = platform.processor()
    lines.append(f"Processor: {model or 'unknown'} ({os.cpu_count()} threads)")
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for l in f:
                k, v = l.split(":", 1)
                mem[k] = int(v.split()[0]) // 1024
        lines.append(f"Memory: {mem.get('MemAvailable', 0)} MB free of {mem.get('MemTotal', 0)} MB")
    except OSError:
        pass
    if hasattr(os, "getloadavg"):
        a, b, c = os.getloadavg()
        lines.append(f"Load (1/5/15 min): {a:.2f} {b:.2f} {c:.2f}")
    gpus = sorted(f for f in os.listdir("/dev/dri") if f.startswith("renderD")) if os.path.isdir("/dev/dri") else []
    lines.append("Graphics: " + (", ".join(f"{g} ({_vendor(g)})" for g in gpus) if gpus else "none found"))
    return lines


def _vendor(node: str) -> str:
    try:
        with open(f"/sys/class/drm/{node}/device/vendor") as f:
            return {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}.get(f.read().strip(), "other")
    except OSError:
        return "unknown"
