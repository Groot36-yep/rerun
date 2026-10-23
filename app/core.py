"""The running app: settings, catalog, schedules and streams, wired together."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path

from . import images, seasons
from .catalog import Catalog, Scanner
from .channels import sync_auto
from .ffmpeg import FFmpeg
from .folders import Prober
from .plex import PlexClient
from .scheduler import Schedules
from .security import LoginLimiter, Sessions, parse_networks
from .store import DATA_DIR, atomic_write_json, load_config, load_secrets, read_json
from .streaming import StreamManager

log = logging.getLogger("rerun")


class LogBuffer(logging.Handler):
    """Keeps the last few hundred log lines for the Status page."""

    def __init__(self, size=400):
        super().__init__(logging.INFO)
        self.lines: deque = deque(maxlen=size)

    def emit(self, record):
        try:
            self.lines.append({"t": record.created, "level": record.levelname.lower(), "msg": record.getMessage()})
        except Exception:
            pass


class Chapters:
    """Chapter marks used to place commercial breaks; looked up once per episode and cached."""

    def __init__(self, app: "App"):
        self.app = app
        self.path = DATA_DIR / "chapters.json"
        self.cache: dict = read_json(self.path, {})
        self.dirty = False
        self._prober: Prober | None = None

    async def get(self, m: dict) -> list[float]:
        if m.get("chapters") is not None:
            return m["chapters"]
        if m["id"] in self.cache:
            return self.cache[m["id"]]
        chs: list[float] = []
        try:
            local = self.app.local_path(m.get("file") or "")
            if local:
                if self._prober is None:
                    self._prober = Prober(self.app.ffmpeg.ffprobe)
                chs = await asyncio.wait_for(self._prober.chapters(local), 20)
            elif m.get("part") and m["id"].startswith("p"):
                plex = self.app.plex()
                if plex:
                    chs = await asyncio.wait_for(plex.chapters(m["id"][1:]), 20)
        except Exception:
            chs = []
        self.cache[m["id"]] = chs
        self.dirty = True
        return chs

    def save(self):
        if self.dirty:
            atomic_write_json(self.path, self.cache)
            self.dirty = False
            if self._prober:
                self._prober.save()


class App:
    def __init__(self, port: int):
        self.port = port
        self.started = time.time()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = load_config()
        self.secrets = load_secrets()
        self.sessions = Sessions()
        self.limiter = LoginLimiter()
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        self.ffmpeg = FFmpeg()
        self.catalog = Catalog.load()
        self.scanner = Scanner(self)
        self.chapters = Chapters(self)
        self.schedules = Schedules(self)
        self.streams = StreamManager(self, port)
        roots = os.environ.get("RERUN_MEDIA_ROOTS", "/media" if os.name != "nt" else "")
        self.media_roots = [str(Path(r).resolve()) for r in roots.split(os.pathsep) if r.strip()]
        self._plex: PlexClient | None = None
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle ----------------------------------------------------------------------
    async def start(self):
        await asyncio.to_thread(self.ffmpeg.detect)
        await self.ffmpeg.self_test(self.cfg.data["stream"])
        self._tasks = [
            asyncio.create_task(self._guard(self.schedules.loop, "scheduler")),
            asyncio.create_task(self._guard(self.streams.reaper, "stream reaper")),
            asyncio.create_task(self._guard(self._daily_rescan, "daily rescan")),
            asyncio.create_task(self._guard(self._ready_logos, "logo maker")),
        ]

    async def stop(self):
        await self.streams.stop_all()
        for t in self._tasks:
            t.cancel()
        self.chapters.save()
        if self._plex:
            await self._plex.aclose()

    async def _guard(self, fn, name):
        """Run a background job forever; if it crashes, log it and start it again."""
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Background job '%s' crashed; restarting it in 30s", name)
                await asyncio.sleep(30)

    async def _ready_logos(self):
        """Have every channel's logo made before Plex asks, so it never waits on a slow download."""
        await asyncio.sleep(5)
        while True:
            online = self.cfg.data["metadata"].get("online_lookups", True)
            for ch in list(self.cfg.data["channels"]):
                try:
                    await images.channel_logo(ch, self.catalog, self.festive(), online)
                except Exception as e:
                    log.warning("Couldn't make the logo for %s: %s", ch.get("name"), e)
            await asyncio.sleep(600)

    async def _daily_rescan(self):
        last_day = None
        while True:
            await asyncio.sleep(300)
            lt = time.localtime()
            if (self.cfg.data.get("setup_complete") and lt.tm_hour == int(self.cfg.data.get("rescan_hour", 4))
                    and last_day != lt.tm_yday):
                last_day = lt.tm_yday
                log.info("Starting the nightly library scan")
                self.scanner.start(manual=False)

    # ---- helpers ------------------------------------------------------------------------
    def set_catalog(self, cat: Catalog):
        self.catalog = cat
        if self.cfg.data.get("setup_complete"):
            made = sync_auto(self.cfg.data, cat)
            self.cfg.save()
            for ch in made:
                log.info("Added channel %s %s (found in your library)", ch["number"], ch["name"])
                asyncio.ensure_future(self.schedules.ensure(ch, time.time() + 6 * 3600))

    def festive(self) -> str:
        """The holiday whose badge goes on channel logos right now, if any."""
        if not self.cfg.data.get("festive_logos", False):
            return ""
        on = seasons.active(self.cfg.data.get("holidays"), time.time())
        return on[0] if on else ""

    def channel_visible(self, ch: dict) -> bool:
        if not ch.get("enabled", True):
            return False
        if ch.get("season") and ch.get("off_season") == "hide":
            return seasons.in_window(self.cfg.data["holidays"].get(ch["season"]) or {}, time.time())
        return True

    def plex(self) -> PlexClient | None:
        url = self.cfg.data["plex"].get("url")
        token = self.secrets.data.get("plex_token")
        if not url or not token:
            return None
        if self._plex is None or self._plex.url != url.rstrip("/") or self._plex.token != token:
            old = self._plex
            self._plex = PlexClient(url, token, self.secrets.data["client_id"])
            if old:
                asyncio.ensure_future(old.aclose())
        return self._plex

    def channel(self, cid: str) -> dict | None:
        return next((c for c in self.cfg.data["channels"] if c["id"] == cid), None)

    def channel_by_number(self, number: int) -> dict | None:
        return next((c for c in self.cfg.data["channels"] if c["number"] == number and c.get("enabled", True)), None)

    def extra_networks(self):
        return parse_networks(self.cfg.data["security"].get("extra_networks"))

    def path_allowed(self, path: str) -> bool:
        if not path:
            return False
        if not self.media_roots:
            return True
        real = os.path.realpath(path)
        return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in self.media_roots)

    def map_path(self, path: str) -> str:
        for m in self.cfg.data.get("path_map") or []:
            src, dst = m.get("from", ""), m.get("to", "")
            if src and path.startswith(src):
                return dst + path[len(src):]
        return path

    def local_path(self, path: str) -> str | None:
        if not path:
            return None
        p = self.map_path(path)
        return p if os.path.isfile(p) else None

    def media_input(self, item: dict) -> str | None:
        """A file path if Rerun can see the file directly, otherwise a URL to fetch it from Plex."""
        local = self.local_path(item.get("f") or "")
        if local:
            return local
        if item.get("p"):
            plex = self.plex()
            if plex:
                return plex.part_url(item["p"])
        return None
