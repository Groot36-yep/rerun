"""Everything Rerun knows about your media, and the scan that builds it.

The catalog is a cache: deleting catalog.json just means the next scan
rebuilds it. Channels refer to shows by rules ("everything on Nickelodeon"),
so new shows you add to Plex join the right channels on the next scan.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from .folders import Prober, parse_music_video, scan_folder
from .metadata import NetworkLookup
from .networks import family_for, movie_family, parent_of
from .plex import TYPE_EPISODE, TYPE_MOVIE, TYPE_SHOW, PlexError, parse_guids, pick_media, tags
from .store import DATA_DIR, atomic_write_json, read_json

log = logging.getLogger("rerun.catalog")


class Catalog:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.scanned_at: float = data.get("scanned_at", 0)
        self.shows: dict[str, dict] = data.get("shows", {})
        self.episodes: dict[str, list[dict]] = data.get("episodes", {})
        self.movies: dict[str, dict] = data.get("movies", {})
        self.music_videos: dict[str, list[dict]] = data.get("music_videos", {})
        self.filler: dict[str, list[dict]] = data.get("filler", {})
        self.networks: dict[str, dict] = data.get("networks", {})
        self.reindex()

    def reindex(self):
        self.items: dict[str, dict] = {}
        for eps in self.episodes.values():
            for e in eps:
                self.items[e["id"]] = e
        self.items.update(self.movies)
        for lst in (*self.music_videos.values(), *self.filler.values()):
            for x in lst:
                self.items[x["id"]] = x

    def to_dict(self) -> dict:
        return {
            "scanned_at": self.scanned_at, "shows": self.shows, "episodes": self.episodes,
            "movies": self.movies, "music_videos": self.music_videos, "filler": self.filler,
            "networks": self.networks,
        }

    @classmethod
    def load(cls) -> "Catalog":
        return cls(read_json(DATA_DIR / "catalog.json", {}))

    def save(self):
        atomic_write_json(DATA_DIR / "catalog.json", self.to_dict())

    def all_music_videos(self) -> list[dict]:
        return [x for lst in self.music_videos.values() for x in lst]

    def all_filler(self) -> list[dict]:
        return [x for lst in self.filler.values() for x in lst]

    def artwork(self, media_id: str) -> str:
        """The Plex poster path for a show or movie id (only those, never arbitrary paths)."""
        x = self.shows.get(media_id) or self.movies.get(media_id)
        return (x or {}).get("thumb") or ""

    def summary(self) -> dict:
        return {
            "shows": len(self.shows),
            "episodes": sum(len(v) for v in self.episodes.values()),
            "movies": len(self.movies),
            "music_videos": sum(len(v) for v in self.music_videos.values()),
            "commercials": sum(len(v) for v in self.filler.values()),
            "scanned_at": self.scanned_at,
        }

    def facets(self) -> dict:
        nets = Counter(s.get("network") for s in self.shows.values() if s.get("network"))
        for m in self.movies.values():
            if m.get("network"):
                nets[m["network"]] += 0  # list it, but count shows only
        genres = Counter(g for s in self.shows.values() for g in s.get("genres", []))
        mgenres = Counter(g for m in self.movies.values() for g in m.get("genres", []))
        ftags = Counter(t for x in self.all_filler() for t in x.get("tags", []))
        mvtags = Counter(t for x in self.all_music_videos() for t in x.get("tags", []))
        show_cols = Counter(c for s in self.shows.values() for c in s.get("collections", []))
        movie_cols = Counter(c for m in self.movies.values() for c in m.get("collections", []))
        return {
            "show_collections": [{"name": k, "count": v} for k, v in sorted(show_cols.items())],
            "movie_collections": [{"name": k, "count": v} for k, v in sorted(movie_cols.items())],
            "networks": [{"name": k, "shows": v} for k, v in nets.most_common()],
            "show_genres": [{"name": k, "count": v} for k, v in genres.most_common()],
            "movie_genres": [{"name": k, "count": v} for k, v in mgenres.most_common()],
            "commercial_tags": [{"name": k, "count": v} for k, v in ftags.most_common()],
            "music_tags": [{"name": k, "count": v} for k, v in mvtags.most_common()],
            "commercial_sources": [{"key": k, "count": len(v)} for k, v in self.filler.items()],
            "music_sources": [{"key": k, "count": len(v)} for k, v in self.music_videos.items()],
        }


def _year(m: dict) -> int:
    if m.get("year"):
        return int(m["year"])
    d = m.get("originallyAvailableAt") or ""
    return int(d[:4]) if d[:4].isdigit() else 0


class Scanner:
    def __init__(self, app):
        self.app = app
        self.progress = {"running": False, "step": "", "done": 0, "total": 0, "error": "", "finished": 0,
                         "summary": {}}
        self._task: asyncio.Task | None = None

    def start(self, manual: bool = True) -> bool:
        if self.progress["running"]:
            return False
        self.progress.update(running=True, error="", step="Starting", done=0, total=0)
        self._task = asyncio.create_task(self._run(manual))
        return True

    def _step(self, text, done=0, total=0):
        self.progress.update(step=text, done=done, total=total)

    async def _run(self, manual: bool):
        try:
            cat = await self.scan()
            before = self.app.catalog.summary()
            had = before["episodes"] + before["movies"]
            has = cat.summary()["episodes"] + cat.summary()["movies"]
            if not manual and had >= 50 and has < had * 0.5:
                # Plex sometimes answers with a half-empty library while it's starting up or a drive
                # isn't mounted yet. Don't let that wipe out every channel overnight.
                log.warning("The nightly scan found %s items instead of %s; keeping the previous library. "
                            "If you really removed that much, press Rescan library.", has, had)
                self.progress["error"] = (f"The last automatic scan found much less than before ({has} vs {had}), "
                                          "so Rerun kept your previous library. Press Rescan library if that's expected.")
                return
            self._step("Saving")
            await asyncio.to_thread(cat.save)
            self.app.set_catalog(cat)
            self.progress["summary"] = cat.summary()
            log.info("Scan finished: %s", cat.summary())
        except PlexError as e:
            self.progress["error"] = str(e)
            log.error("Scan failed: %s", e)
        except Exception as e:
            log.exception("Scan failed")
            self.progress["error"] = f"Scan failed: {e.__class__.__name__}: {e}"
        finally:
            self.progress.update(running=False, finished=time.time(), step="Done")

    async def scan(self) -> Catalog:
        cfg = self.app.cfg.data
        sources = cfg["sources"]
        old = self.app.catalog
        cat = Catalog()
        include_specials = cfg["metadata"].get("include_specials", False)
        plex_needed = any(s.get("type") == "plex" for lst in sources.values() for s in lst)
        plex = self.app.plex() if plex_needed else None
        if plex_needed and plex is None:
            raise PlexError("Plex isn't connected yet. Connect it in Settings first.")

        if plex:
            for src in sources["shows"]:
                if src.get("type") != "plex":
                    continue
                key = f"plex:{src['id']}"
                self._step(f"Reading TV shows from “{src.get('name', key)}”")
                async for m in plex.items(src["id"], TYPE_SHOW):
                    sid = "p" + str(m["ratingKey"])
                    cat.shows[sid] = {
                        "id": sid, "title": m.get("title", ""), "year": _year(m),
                        "genres": tags(m, "Genre"), "studio": m.get("studio", ""), "network": "",
                        "rating": m.get("contentRating", ""), "thumb": m.get("thumb", ""),
                        "summary": m.get("summary", ""), "guids": parse_guids(m), "src": key, "collections": [],
                    }
                for sid, cols in (await plex.collections(src["id"])).items():
                    if sid in cat.shows:
                        cat.shows[sid]["collections"] = cols
                n = 0
                async for m in plex.items(src["id"], TYPE_EPISODE):
                    n += 1
                    if n % 500 == 0:
                        self._step(f"Reading episodes from “{src.get('name', key)}”", n, 0)
                    sid = "p" + str(m.get("grandparentRatingKey", ""))
                    season = int(m.get("parentIndex") or 0)
                    if sid not in cat.shows or (season == 0 and not include_specials):
                        continue
                    media = pick_media(m)
                    if not media or media["dur"] < 30:
                        continue
                    cat.episodes.setdefault(sid, []).append({
                        "id": "p" + str(m["ratingKey"]), "show": sid, "title": m.get("title", ""),
                        "season": season, "episode": int(m.get("index") or 0),
                        "summary": m.get("summary", ""), "date": m.get("originallyAvailableAt", ""),
                        **media,
                    })

            for src in sources["movies"]:
                if src.get("type") != "plex":
                    continue
                self._step(f"Reading movies from “{src.get('name', '')}”")
                async for m in plex.items(src["id"], TYPE_MOVIE):
                    media = pick_media(m)
                    if not media or media["dur"] < 60:
                        continue
                    mid = "p" + str(m["ratingKey"])
                    cat.movies[mid] = {
                        "id": mid, "title": m.get("title", ""), "year": _year(m), "genres": tags(m, "Genre"),
                        "studio": m.get("studio", ""), "network": movie_family(m.get("studio")),
                        "rating": m.get("contentRating", ""), "thumb": m.get("thumb", ""),
                        "summary": m.get("summary", ""), "date": m.get("originallyAvailableAt", ""),
                        "src": f"plex:{src['id']}", "collections": [], **media,
                    }
                for mid, cols in (await plex.collections(src["id"])).items():
                    if mid in cat.movies:
                        cat.movies[mid]["collections"] = cols

            for kind in ("music_videos", "commercials"):
                for src in sources[kind]:
                    if src.get("type") != "plex":
                        continue
                    key = f"plex:{src['id']}"
                    self._step(f"Reading “{src.get('name', key)}”")
                    out = cat.music_videos if kind == "music_videos" else cat.filler
                    lst = out.setdefault(key, [])
                    async for m in plex.items(src["id"], TYPE_MOVIE):
                        media = pick_media(m)
                        if not media or media["dur"] < 1:
                            continue
                        item = {"id": "p" + str(m["ratingKey"]), "tags": [], "src": key, **media}
                        if kind == "music_videos":
                            item.update(parse_music_video(m.get("title", ""), []))
                            item["year"] = item["year"] or _year(m)
                        else:
                            item["title"] = m.get("title", "")
                        lst.append(item)

        folder_srcs = [(k, s) for k in ("music_videos", "commercials") for s in sources[k] if s.get("type") == "folder"]
        if folder_srcs:
            prober = Prober(self.app.ffmpeg.ffprobe)
            for kind, src in folder_srcs:
                path = src.get("path", "")
                if not self.app.path_allowed(path):
                    log.warning("Skipping folder outside your media folders: %s", path)
                    continue
                label = "music videos" if kind == "music_videos" else "commercials"
                self._step(f"Checking {label} in {path}")
                items = await scan_folder(
                    path, kind, prober, lambda d, t, p=path, l=label: self._step(f"Checking {l} in {p}", d, t)
                )
                (cat.music_videos if kind == "music_videos" else cat.filler)[f"folder:{path}"] = items
            prober.save()

        # Drop shows with nothing playable, and sort episodes in airing order.
        for sid in list(cat.shows):
            eps = cat.episodes.get(sid)
            if not eps:
                cat.shows.pop(sid)
                cat.episodes.pop(sid, None)
                continue
            eps.sort(key=lambda e: (e["season"], e["episode"], e["title"]))

        await self._networks(cat, old)
        cat.scanned_at = time.time()
        cat.reindex()
        return cat

    async def _networks(self, cat: Catalog, old: Catalog):
        cfg = self.app.cfg.data
        online = cfg["metadata"].get("online_lookups", True)
        key = self.app.secrets.data.get("tmdb_key", "")
        shows = list(cat.shows.values())
        self._step("Working out which network each show aired on", 0, len(shows))
        done = 0
        sem = asyncio.Semaphore(6)
        async with NetworkLookup(key, online) as nl:

            async def one(s):
                nonlocal done
                async with sem:
                    try:
                        net, logo = await nl.show_network(s["guids"], s.get("studio", ""))
                    except Exception as e:
                        log.warning("Network lookup failed for %s: %s", s["title"], e)
                        prev = old.shows.get(s["id"], {})
                        net, logo = prev.get("network_raw", s.get("studio", "")), ""
                fam, category = family_for(net)
                s["network"], s["network_raw"], s["network_parent"] = fam, net, parent_of(fam)
                if fam:
                    entry = cat.networks.setdefault(fam, {"logo": "", "category": category})
                    if logo and (not entry["logo"] or net.strip().lower() == fam.lower()):
                        entry["logo"] = logo
                done += 1
                self._step("Working out which network each show aired on", done, len(shows))

            await asyncio.gather(*(one(s) for s in shows))
        for m in cat.movies.values():
            if m.get("network"):
                cat.networks.setdefault(m["network"], {"logo": "", "category": family_for(m["network"])[1]})
